from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .retrieval import TextRetriever
from .sandbox import SandboxProvider, create_sandbox


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilePathArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=512)


class DirectoryPathArguments(ToolArguments):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(default=".", min_length=1, max_length=512)


class WriteFileArguments(FilePathArguments):
    content: str = Field(max_length=1_000_000)


class ReplaceTextArguments(FilePathArguments):
    old_text: str = Field(min_length=1, max_length=100_000)
    new_text: str = Field(max_length=100_000)
    expected_replacements: int = Field(default=1, ge=1, le=100)


class SearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)


class CommandArguments(ToolArguments):
    command: str = Field(min_length=1, max_length=500)


class LogArguments(ToolArguments):
    limit: int = Field(default=10, ge=1, le=20)


@dataclass(frozen=True)
class ToolExecution:
    name: str
    ok: bool
    content: str
    audit_input: dict[str, object]
    changed_files: tuple[str, ...] = ()
    duration_ms: int = 0


class AgentToolbox:
    """Validated repository tools whose command execution always uses a sandbox."""

    max_read_bytes = 256 * 1024
    max_output_chars = 20_000
    max_directory_entries = 200
    ignored_names = {
        ".git",
        ".env",
        ".env.local",
        ".env.production",
        ".aws",
        ".ssh",
        "id_rsa",
        "id_ed25519",
        "credentials",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
    }
    protected_paths = {
        ".tender/evaluator",
        ".tender/benchmark.toml",
        "benchmarks/tasks.jsonl",
    }
    dependency_files = {
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }

    tool_specs: tuple[tuple[str, str, type[ToolArguments]], ...] = (
        ("read_file", "Read a bounded UTF-8 repository file.", FilePathArguments),
        ("write_file", "Create or replace a repository file.", WriteFileArguments),
        (
            "replace_text",
            "Replace an exact number of text occurrences in a file.",
            ReplaceTextArguments,
        ),
        (
            "list_directory",
            "List bounded entries in a repository directory. Use path '.' for the repository root.",
            DirectoryPathArguments,
        ),
        ("search_code", "Search relevant source lines using lexical ranking.", SearchArguments),
        (
            "run_command",
            "Run a bounded shell command inside the selected sandbox.",
            CommandArguments,
        ),
        (
            "run_tests",
            "Run configured setup and target-test commands in the sandbox.",
            ToolArguments,
        ),
        ("git_diff", "Read the current tracked and untracked patch summary.", ToolArguments),
        ("git_status", "Read sanitized Git working-tree status.", ToolArguments),
        ("git_log", "Read a bounded list of recent commit subjects.", LogArguments),
        (
            "dependency_lookup",
            "Find a dependency name in common project manifests.",
            SearchArguments,
        ),
        ("lint", "Run only the static-analysis commands configured for this task.", ToolArguments),
    )

    def __init__(
        self,
        root: str | Path,
        *,
        sandbox_backend: str,
        timeout_seconds: int,
        test_commands: list[str],
        setup_commands: list[str],
        static_analysis_commands: list[str],
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        self.sandbox_backend = sandbox_backend
        self.timeout_seconds = timeout_seconds
        self.test_commands = test_commands
        self.setup_commands = setup_commands
        self.static_analysis_commands = static_analysis_commands
        self._sandbox: SandboxProvider | None = None
        self._retriever = TextRetriever()

    @classmethod
    def definitions(cls) -> list[dict[str, object]]:
        definitions = []
        for name, description, argument_type in cls.tool_specs:
            parameters = argument_type.model_json_schema()
            if name == "list_directory":
                parameters["properties"]["path"]["default"] = "."
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                }
            )
        return definitions

    def execute(self, name: str, arguments: str | dict[str, Any]) -> ToolExecution:
        started = perf_counter()
        spec = next((item for item in self.tool_specs if item[0] == name), None)
        if spec is None:
            return self._error(name, {}, "unknown tool", started)
        try:
            raw = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(raw, dict):
                raise ValueError("arguments must be a JSON object")
            validated = spec[2].model_validate(raw)
            safe_input = self._audit_input(name, validated.model_dump())
            value = getattr(self, f"_{name}")(**validated.model_dump())
            if isinstance(value, tuple):
                content, changed_files = value
            else:
                content, changed_files = value, ()
            serialized = (
                content if isinstance(content, str) else json.dumps(content, sort_keys=True)
            )
            if isinstance(content, dict):
                ok = bool(content.get("passed", True))
            elif isinstance(content, list):
                ok = all(
                    not isinstance(item, dict) or bool(item.get("passed", True)) for item in content
                )
            else:
                ok = True
            return ToolExecution(
                name,
                ok,
                serialized[: self.max_output_chars],
                safe_input,
                tuple(changed_files),
                round((perf_counter() - started) * 1000),
            )
        except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
            return self._error(
                name,
                self._audit_input(name, raw if "raw" in locals() else {}),
                str(exc),
                started,
            )

    def close(self) -> None:
        if self._sandbox is not None:
            self._sandbox.destroy()
            self._sandbox = None

    def _read_file(self, path: str) -> str:
        target = self._path(path, write=False)
        if not target.is_file():
            raise ValueError("path is not a file")
        if target.stat().st_size > self.max_read_bytes:
            raise ValueError("file exceeds the 256 KiB read limit")
        text = target.read_text(encoding="utf-8")
        return text[: self.max_output_chars]

    def _write_file(self, path: str, content: str) -> tuple[dict[str, object], tuple[str, ...]]:
        target = self._path(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8")
        file_mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
        fd, temporary = tempfile.mkstemp(prefix=".tender-write-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
            os.chmod(temporary, file_mode)
            os.replace(temporary, target)
            self.close()
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        relative = target.relative_to(self.root).as_posix()
        return (
            {
                "written": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            (relative,),
        )

    def _replace_text(
        self, path: str, old_text: str, new_text: str, expected_replacements: int
    ) -> tuple[dict[str, object], tuple[str, ...]]:
        target = self._path(path, write=True)
        text = target.read_text(encoding="utf-8")
        actual = text.count(old_text)
        if actual != expected_replacements:
            raise ValueError(
                f"expected {expected_replacements} occurrence(s) but found {actual}; file unchanged"
            )
        updated = text.replace(old_text, new_text)
        self._write_file(path, updated)
        return (
            {"replaced": actual, "path": target.relative_to(self.root).as_posix()},
            (target.relative_to(self.root).as_posix(),),
        )

    def _list_directory(self, path: str) -> list[dict[str, object]]:
        target = self._path(path, write=False)
        if not target.is_dir():
            raise ValueError("path is not a directory")
        entries = []
        for item in sorted(target.iterdir(), key=lambda entry: entry.name.casefold()):
            if (
                self._sensitive_name(item.name)
                or self._protected_path(item.relative_to(self.root).as_posix())
                or item.is_symlink()
            ):
                continue
            entries.append(
                {
                    "name": item.name,
                    "kind": "directory" if item.is_dir() else "file",
                    "size_bytes": item.stat().st_size if item.is_file() else None,
                }
            )
            if len(entries) >= self.max_directory_entries:
                break
        return entries

    def _search_code(self, query: str, limit: int) -> list[dict[str, object]]:
        results = []
        for hit in self._retriever.search(self.root, query, limit=min(limit * 4, 80)):
            if self._protected_path(hit.path) or any(
                self._sensitive_name(part) for part in Path(hit.path).parts
            ):
                continue
            results.append(
                {
                    "path": hit.path,
                    "line": hit.line,
                    "text": hit.text,
                    "score": hit.score,
                    "reason": hit.reason,
                }
            )
            if len(results) == limit:
                break
        return results

    def _run_command(self, command: str) -> dict[str, object]:
        return self._command_result(self._get_sandbox().run(command))

    def _run_tests(self) -> list[dict[str, object]]:
        return self._run_configured(self.setup_commands + self.test_commands)

    def _lint(self) -> list[dict[str, object]]:
        if not self.static_analysis_commands:
            raise ValueError("no static-analysis commands are configured for this task")
        return self._run_configured(self.setup_commands + self.static_analysis_commands)

    def _git_diff(self) -> str:
        tracked = self._git(["diff", "--no-ext-diff", "--no-textconv", "HEAD", "--"])
        untracked = self._git(["ls-files", "--others", "--exclude-standard"])
        return json.dumps(
            {"diff": tracked[:12000], "untracked_files": untracked.splitlines()[:200]}
        )

    def _git_status(self) -> str:
        return self._git(["status", "--short", "--untracked-files=all"])[:12000]

    def _git_log(self, limit: int) -> list[str]:
        return self._git(["log", f"-{limit}", "--format=%h %s"]).splitlines()

    def _dependency_lookup(self, query: str, limit: int) -> list[dict[str, object]]:
        term = query.casefold()
        matches: list[dict[str, object]] = []
        for current, dirs, names in os.walk(self.root, followlinks=False):
            parent = Path(current)
            dirs[:] = [
                name
                for name in dirs
                if name.casefold() not in self.ignored_names
                and not name.casefold().startswith(".env")
                and not (parent / name).is_symlink()
            ]
            for name in sorted(names):
                if name not in self.dependency_files:
                    continue
                path = parent / name
                try:
                    if path.is_symlink() or path.stat().st_size > 256 * 1024:
                        continue
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if term in line.casefold():
                        matches.append(
                            {
                                "path": path.relative_to(self.root).as_posix(),
                                "line": line_number,
                                "text": line.strip()[:500],
                            }
                        )
                        if len(matches) >= limit:
                            return matches
        return matches

    def _run_configured(self, commands: list[str]) -> list[dict[str, object]]:
        results = []
        for command in commands:
            result = self._get_sandbox().run(command)
            results.append(self._command_result(result))
            if not result.passed:
                break
        return results

    def _get_sandbox(self) -> SandboxProvider:
        if self._sandbox is None:
            self._sandbox = create_sandbox(self.root, self.sandbox_backend, self.timeout_seconds)
        return self._sandbox

    def _git(self, arguments: list[str]) -> str:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
        }
        completed = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=min(self.timeout_seconds, 15),
        )
        if completed.returncode:
            raise ValueError(completed.stderr.strip()[:1000] or "Git command failed")
        return completed.stdout

    def _path(self, raw: str, *, write: bool) -> Path:
        if raw == "." and not write:
            return self.root
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("path must remain inside the repository")
        if not relative.parts and write:
            raise ValueError("a repository-relative path is required")
        if any(self._sensitive_name(part) for part in relative.parts):
            raise ValueError("access to credential and Git metadata paths is denied")
        normalized = relative.as_posix()
        if self._protected_path(normalized):
            raise ValueError("access to evaluator and benchmark definition files is denied")
        candidate = self.root / relative
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path resolves outside the repository") from exc
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symbolic links are not accessible through agent tools")
        return resolved

    @classmethod
    def _protected_path(cls, path: str) -> bool:
        normalized = path.casefold()
        return any(
            normalized == protected.casefold()
            or normalized.startswith(protected.casefold().rstrip("/") + "/")
            for protected in cls.protected_paths
        )

    @classmethod
    def _sensitive_name(cls, name: str) -> bool:
        normalized = name.casefold()
        if normalized in cls.ignored_names or normalized.startswith(".env"):
            return True
        if "credential" in normalized or "secret" in normalized:
            return True
        if normalized.startswith("token.") or normalized.endswith((".token", "_token", "-token")):
            return True
        return Path(normalized).suffix in {".pem", ".key", ".p12", ".pfx", ".keystore"}

    @staticmethod
    def _command_result(result) -> dict[str, object]:
        return {
            "command": result.command,
            "passed": result.passed,
            "return_code": result.return_code,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        }

    @staticmethod
    def _audit_input(name: str, arguments: object) -> dict[str, object]:
        if not isinstance(arguments, dict):
            return {"arguments_type": type(arguments).__name__}
        safe = dict(arguments)
        if name == "write_file" and isinstance(safe.get("content"), str):
            content = safe.pop("content")
            safe.update(
                {
                    "content_bytes": len(content.encode("utf-8")),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                }
            )
        if name == "replace_text":
            for key in ("old_text", "new_text"):
                content = safe.pop(key, None)
                if isinstance(content, str):
                    safe[f"{key}_bytes"] = len(content.encode("utf-8"))
                    safe[f"{key}_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return safe

    @staticmethod
    def _error(name: str, arguments: object, message: str, started: float) -> ToolExecution:
        return ToolExecution(
            name,
            False,
            json.dumps({"error": message[:1000]}),
            AgentToolbox._audit_input(name, arguments),
            duration_ms=round((perf_counter() - started) * 1000),
        )
