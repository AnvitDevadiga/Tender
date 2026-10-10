from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .models import Issue
from .workflow import WorkflowResult


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    issue: Issue
    workspace: str
    base_commit: str
    setup_commands: tuple[str, ...]
    test_commands: tuple[str, ...]
    verification_tests: tuple[str, ...]
    timeout_seconds: int
    static_analysis_commands: tuple[str, ...] = ()

    @property
    def commands(self) -> tuple[str, ...]:
        """Compatibility alias for the commands used during agent iteration."""
        return self.test_commands

    @classmethod
    def load_jsonl(cls, path: str | Path) -> list["BenchmarkTask"]:
        tasks: list[BenchmarkTask] = []
        for line_number, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("task record must be a JSON object")
                if data.get("schema_version") != 2:
                    raise ValueError("schema_version must be 2")
                task_id = str(data["task_id"]).strip()
                issue = Issue.model_validate(data["issue"])
                workspace = str(data["workspace"])
                base_commit = str(data["base_commit"])
                setup_commands = cls._commands(data.get("setup_commands", []), "setup_commands")
                test_commands = cls._commands(data["test_commands"], "test_commands")
                verification_tests = cls._commands(data["verification_tests"], "verification_tests")
                timeout_seconds = int(data["timeout_seconds"])
                static_analysis_commands = cls._commands(
                    data.get("static_analysis_commands", []), "static_analysis_commands"
                )
                if not task_id or not workspace:
                    raise ValueError("task_id and workspace are required")
                if not re.fullmatch(r"[0-9a-fA-F]{40}", base_commit):
                    raise ValueError("base_commit must be a full 40-character Git commit SHA")
                if issue.base_commit and issue.base_commit.lower() != base_commit.lower():
                    raise ValueError("issue.base_commit must match task base_commit")
                if not test_commands or not verification_tests:
                    raise ValueError("test_commands and verification_tests are required")
                if not 1 <= timeout_seconds <= 3600:
                    raise ValueError("timeout_seconds must be between 1 and 3600")
                issue.base_commit = base_commit
                tasks.append(
                    cls(
                        task_id,
                        issue,
                        workspace,
                        base_commit,
                        setup_commands,
                        test_commands,
                        verification_tests,
                        timeout_seconds,
                        static_analysis_commands,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid benchmark task at line {line_number}: {exc}") from exc
        return tasks

    @staticmethod
    def _commands(value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or any(
            not isinstance(command, str) or not command.strip() for command in value
        ):
            raise ValueError(f"{field} must be an array of non-empty command strings")
        return tuple(value)


@dataclass(frozen=True)
class ExperimentManifest:
    name: str
    dataset: str
    model: str
    architecture: str
    retrieval: str
    repetitions: int
    max_tokens: int
    max_minutes: int
    retry_budgets: tuple[int, ...]
    sandbox: str = "local"
    timeout_seconds: int = 120
    input_usd_per_million_tokens: float | None = None
    output_usd_per_million_tokens: float | None = None
    pricing_basis: str | None = None
    retrieval_limits: tuple[int, ...] = (12,)

    @classmethod
    def load(cls, path: str | Path) -> "ExperimentManifest":
        with Path(path).open("rb") as file:
            data = tomllib.load(file)
        manifest = cls(
            name=data["name"],
            dataset=data["dataset"],
            model=data["model"],
            architecture=data["architecture"],
            retrieval=data["retrieval"],
            repetitions=int(data["repetitions"]),
            max_tokens=int(data["max_tokens"]),
            max_minutes=int(data["max_minutes"]),
            retry_budgets=tuple(data["retry_budgets"]),
            sandbox=str(data.get("sandbox", "local")),
            timeout_seconds=int(data.get("timeout_seconds", 120)),
            input_usd_per_million_tokens=(
                float(data["input_usd_per_million_tokens"])
                if data.get("input_usd_per_million_tokens") is not None
                else None
            ),
            output_usd_per_million_tokens=(
                float(data["output_usd_per_million_tokens"])
                if data.get("output_usd_per_million_tokens") is not None
                else None
            ),
            pricing_basis=(
                str(data["pricing_basis"]).strip() if data.get("pricing_basis") else None
            ),
            retrieval_limits=tuple(int(limit) for limit in data.get("retrieval_limits", [12])),
        )
        if (
            not manifest.name.strip()
            or manifest.repetitions < 1
            or manifest.max_tokens < 1
            or manifest.max_minutes < 1
        ):
            raise ValueError(
                "manifest name, repetitions, max_tokens, and max_minutes must be positive"
            )
        if not manifest.retry_budgets or any(int(budget) < 0 for budget in manifest.retry_budgets):
            raise ValueError("manifest retry_budgets must contain a non-negative budget")
        if not manifest.retrieval_limits or any(
            not 1 <= limit <= 50 for limit in manifest.retrieval_limits
        ):
            raise ValueError("manifest retrieval_limits must contain values between 1 and 50")
        if manifest.sandbox not in {"local", "docker"}:
            raise ValueError("manifest sandbox must be local or docker")
        if (manifest.input_usd_per_million_tokens is None) != (
            manifest.output_usd_per_million_tokens is None
        ):
            raise ValueError("both published token prices must be provided together")
        if manifest.input_usd_per_million_tokens is not None and (
            manifest.input_usd_per_million_tokens < 0
            or manifest.output_usd_per_million_tokens is None
            or manifest.output_usd_per_million_tokens < 0
            or not manifest.pricing_basis
        ):
            raise ValueError(
                "published token prices require non-negative values and a pricing basis"
            )
        if not 1 <= manifest.timeout_seconds <= 3600:
            raise ValueError("manifest timeout_seconds must be between 1 and 3600")
        return manifest


class ExperimentRunner:
    def __init__(self, manifest: ExperimentManifest) -> None:
        self.manifest = manifest

    async def run(
        self, task_factory: Callable[[int], Awaitable[WorkflowResult]]
    ) -> list[WorkflowResult]:
        results: list[WorkflowResult] = []
        for retry_budget in self.manifest.retry_budgets:
            for _ in range(self.manifest.repetitions):
                results.append(await task_factory(retry_budget))
        return results
