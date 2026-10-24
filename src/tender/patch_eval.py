"""Evaluate an existing patch against a pinned commit with independent criteria.

    repository + base commit + patch + acceptance criteria
      → validate the patch (syntax, paths, sizes, binaries, symlinks, integrity)
      → baseline: target checks on the unpatched base (do the checks discriminate?)
      → target checks: fresh workspace, patch applied, evaluator files reset
      → regression checks: another fresh workspace
      → report.json, report.md, and a hash-sealed evidence bundle

The source repository is only read (see ``tender.snapshot``). Criteria are
loaded, and hidden evaluator assets read, before the patch is touched, so the
patch cannot redefine how it is judged. Untrusted patches run only in the
Docker sandbox; the local backend requires an explicit trust flag and is
recorded as "no isolation" in the report.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .evidence import EvidenceWriter, environment_identity, sanitize, sanitize_text
from .integrity import (
    EvaluatorAssets,
    IntegrityPolicy,
    check_patch_paths,
    restore_evaluator_paths,
)
from .outcomes import CheckOutcome, EvaluationOutcome
from .repository import RepositoryError
from .sandbox import (
    CommandResult,
    ExecutionBudget,
    SandboxUnavailable,
    create_sandbox,
    current_budget,
)
from .snapshot import RepositoryBoundaryError, TrustedRepository, run_safe_git

REPORT_SCHEMA = "tender.patch_report.v1"
JUNIT_MARKER = "----TENDER-JUNIT-REPORT----"
_NODE_ID = re.compile(r"^[A-Za-z0-9_./\-]+(?:::[A-Za-z0-9_\[\]\-.,=: ]+)?$")


# ------------------------------------------------------------------ criteria


class CheckSpec(BaseModel):
    """One evaluator-owned check: a pytest selection (preferred) or a command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    pytest: list[str] | None = Field(default=None, max_length=200)
    command: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _one_kind(self) -> CheckSpec:
        if (self.pytest is None) == (self.command is None):
            raise ValueError(f"check {self.name!r} needs exactly one of 'pytest' or 'command'")
        for node in self.pytest or []:
            if not _NODE_ID.fullmatch(node) or ".." in node.split("::")[0].split("/"):
                raise ValueError(f"check {self.name!r} has an invalid pytest selector: {node!r}")
        return self


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=20_000)


class ConstraintSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    deadline_seconds: int = Field(default=900, ge=1, le=86_400)
    max_patch_bytes: int = Field(default=1024 * 1024, ge=1, le=16 * 1024 * 1024)
    max_files: int = Field(default=100, ge=1, le=10_000)
    allow_binary: bool = False
    check_baseline: bool = True
    python: str = Field(default="python3", pattern=r"^[A-Za-z0-9_./\-]+$", max_length=300)


class IntegritySpec(IntegrityPolicy):
    #: Directory of hidden evaluator files, relative to the criteria file.
    hidden_tests: str | None = Field(default=None, max_length=500)


class AcceptanceCriteria(BaseModel):
    """Independent verification criteria, fixed before the candidate runs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    task: TaskSpec
    setup: list[str] = Field(default_factory=list, max_length=20)
    target: list[CheckSpec] = Field(min_length=1, max_length=50)
    regression: list[CheckSpec] = Field(default_factory=list, max_length=50)
    integrity: IntegritySpec = Field(default_factory=IntegritySpec)
    constraints: ConstraintSpec = Field(default_factory=ConstraintSpec)

    @field_validator("setup")
    @classmethod
    def _setup(cls, value: list[str]) -> list[str]:
        if any(not command.strip() or len(command) > 2000 for command in value):
            raise ValueError("setup commands must be non-empty strings")
        return value

    @classmethod
    def load(cls, path: str | Path) -> tuple[AcceptanceCriteria, bytes]:
        raw = Path(path).read_bytes()
        data = (
            tomllib.loads(raw.decode("utf-8")) if str(path).endswith(".toml") else json.loads(raw)
        )
        return cls.model_validate(data), raw


# ------------------------------------------------------------- patch parsing


@dataclass
class PatchFile:
    old_path: str | None
    new_path: str | None
    status: str = "modified"
    binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None
    additions: int = 0
    deletions: int = 0
    index: str | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "status": self.status,
            "binary": self.binary,
            "mode": self.new_mode or self.old_mode,
            "additions": self.additions,
            "deletions": self.deletions,
        }


def _strip_prefix(path: str, prefix: str) -> str | None:
    path = path.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith('"'):
        raise ValueError("quoted (escaped) patch paths are not supported")
    return path[2:] if path.startswith(prefix) else path


def parse_patch(text: str) -> list[PatchFile]:
    """Parse a unified/Git diff into files; raises ValueError when malformed."""
    files: list[PatchFile] = []
    current: PatchFile | None = None
    old_left = new_left = 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for number, line in enumerate(lines, start=1):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                new_left -= 1
                current.additions += 1  # type: ignore[union-attr]
            elif line.startswith("-"):
                old_left -= 1
                current.deletions += 1  # type: ignore[union-attr]
            elif line.startswith(" ") or line == "":
                old_left -= 1
                new_left -= 1
            elif line.startswith("\\"):
                pass
            else:
                raise ValueError(f"line {number}: unexpected content inside a hunk")
            if old_left < 0 or new_left < 0:
                raise ValueError(f"line {number}: hunk is longer than its header says")
            continue
        if line.startswith("\\") and current is not None:
            continue
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)", line)
            if not match:
                raise ValueError(f"line {number}: unsupported diff header (paths with spaces?)")
            current = PatchFile(match.group(1), match.group(2))
            files.append(current)
            continue
        if line.startswith("--- ") and (current is None or current.additions + current.deletions):
            current = PatchFile(None, None)
            files.append(current)
        if current is None:
            continue  # mail headers or prose before the first diff header are ignored
        if line.startswith("new file mode "):
            current.status, current.new_mode = "added", line.split()[-1]
        elif line.startswith("deleted file mode "):
            current.status, current.old_mode = "deleted", line.split()[-1]
        elif line.startswith("old mode "):
            current.old_mode = line.split()[-1]
        elif line.startswith("new mode "):
            current.new_mode = line.split()[-1]
        elif line.startswith(("rename from ", "copy from ")):
            current.status = "renamed" if line.startswith("rename") else "copied"
            current.old_path = line.split(" ", 2)[2]
        elif line.startswith(("rename to ", "copy to ")):
            current.new_path = line.split(" ", 2)[2]
        elif line.startswith("index "):
            current.index = line[6:].split()[0]
            mode = line.split()[-1] if len(line.split()) > 2 else None
            if mode:
                current.new_mode = current.new_mode or mode
        elif line.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
        elif line.startswith("--- "):
            old = _strip_prefix(line[4:], "a/")
            current.old_path = old if old is not None else current.old_path
            if old is None:
                current.status = "added"
        elif line.startswith("+++ "):
            new = _strip_prefix(line[4:], "b/")
            if new is None:
                current.status = "deleted"
                current.new_path = None
            else:
                current.new_path = new
        elif line.startswith("@@"):
            header = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not header:
                raise ValueError(f"line {number}: malformed hunk header")
            old_left = 1 if header.group(2) is None else int(header.group(2))
            new_left = 1 if header.group(4) is None else int(header.group(4))
        elif line.startswith(("similarity index", "dissimilarity index")):
            continue
    if old_left > 0 or new_left > 0:
        raise ValueError("patch ends inside a hunk (truncated patch)")
    return [item for item in files if item.path]


@dataclass
class ValidationResult:
    checks: list[dict[str, Any]] = field(default_factory=list)
    files: list[PatchFile] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check["passed"] for check in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})


def validate_patch(
    raw: bytes,
    criteria: AcceptanceCriteria,
    assets: EvaluatorAssets | None,
) -> ValidationResult:
    """Static validation; nothing from the patch is executed or applied here."""
    result = ValidationResult()
    limits = criteria.constraints
    result.add(
        "size limit",
        len(raw) <= limits.max_patch_bytes,
        f"{len(raw)} bytes (limit {limits.max_patch_bytes})",
    )
    try:
        text = raw.decode("utf-8")
        result.add("UTF-8 text", "\x00" not in text, "")
    except UnicodeDecodeError:
        result.add("UTF-8 text", False, "patch is not valid UTF-8")
        return result
    try:
        files = parse_patch(text)
        result.add("unified diff syntax", bool(files), f"{len(files)} files")
    except ValueError as exc:
        result.add("unified diff syntax", False, str(exc))
        return result
    result.files = files
    if not files:
        return result
    result.add(
        "file count", len(files) <= limits.max_files, f"{len(files)} (limit {limits.max_files})"
    )
    bad_paths = []
    for item in files:
        for path in filter(None, (item.old_path, item.new_path)):
            parts = PurePosixPath(path).parts
            if (
                path.startswith("/")
                or "\\" in path
                or ".." in parts
                or any(part.casefold() == ".git" for part in parts)
                or any(ord(character) < 32 for character in path)
            ):
                bad_paths.append(path)
    result.add(
        "paths stay inside the repository",
        not bad_paths,
        ", ".join(sorted(set(bad_paths))[:10]),
    )
    links = [item.path for item in files if "120000" in {item.old_mode, item.new_mode}]
    result.add("no symbolic links", not links, ", ".join(links[:10]))
    gitlinks = [item.path for item in files if "160000" in {item.old_mode, item.new_mode}]
    result.add("no submodule changes", not gitlinks, ", ".join(gitlinks[:10]))
    binaries = [item.path for item in files if item.binary]
    result.add(
        "binary policy",
        limits.allow_binary or not binaries,
        ("binary changes: " + ", ".join(binaries[:10])) if binaries else "no binary changes",
    )
    changed = sorted({path for item in files for path in (item.old_path, item.new_path) if path})
    findings = check_patch_paths(changed, criteria.integrity, assets)
    result.add(
        "no protected or evaluator-owned paths",
        not findings,
        ", ".join(f"{item.path} ({item.rule})" for item in findings[:10]),
    )
    return result


# ------------------------------------------------------------- check running


def _check_command(spec: CheckSpec, python: str, nonce: str) -> tuple[str, str | None]:
    if spec.command is not None:
        return spec.command, None
    report = f".tender-junit-{nonce}.xml"
    selectors = " ".join(_shell_quote(node) for node in spec.pytest or [])
    command = (
        f"{python} -m pytest -q -p no:cacheprovider -o addopts= -o junit_family=xunit1 "
        f"--junitxml={report} {selectors}; status=$?; echo '{JUNIT_MARKER}'; "
        f"cat {report} 2>/dev/null; exit $status"
    )
    return command, report


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _parse_junit(stdout: str, selectors: list[str]) -> tuple[list[dict[str, str]], str]:
    """Return per-selector outcomes and stdout without the embedded report."""
    before, marker, after = stdout.partition(JUNIT_MARKER)
    outcomes: list[dict[str, str]] = []
    cases: list[tuple[str, str, str]] = []
    if marker and after.strip():
        try:
            root = ElementTree.fromstring(after.strip())
            for case in root.iter("testcase"):
                status = "passed"
                if case.find("failure") is not None or case.find("error") is not None:
                    status = "failed"
                elif case.find("skipped") is not None:
                    status = "skipped"
                cases.append((case.get("file", ""), case.get("name", ""), status))
        except ElementTree.ParseError:
            cases = []
    for selector in selectors:
        path, _, name = selector.partition("::")
        matching = [
            (case_name, status)
            for file, case_name, status in cases
            if file == path and (not name or case_name == name.split("::")[-1])
        ]
        matched = [status for _, status in matching]
        failing = [f"{path}::{case}" for case, status in matching if status != "passed"]
        if not matched:
            outcome = "missing"
        elif all(status == "passed" for status in matched):
            outcome = "passed"
        elif "failed" in matched:
            outcome = "failed"
        else:
            outcome = "skipped"
        entry = {"id": selector, "outcome": outcome, "cases": str(len(matched))}
        if name == "" and failing:
            entry["failing_cases"] = ", ".join(failing[:20])
        outcomes.append(entry)
    return outcomes, before


def _run_group(
    kind: str,
    repository: TrustedRepository,
    base: str,
    patch_text: str | None,
    changed: list[str],
    checks: list[CheckSpec],
    criteria: AcceptanceCriteria,
    assets: EvaluatorAssets | None,
    backend: str,
    python: str,
) -> dict[str, Any]:
    """Run one check group in a brand-new workspace; the workspace is then deleted."""
    group: dict[str, Any] = {"kind": kind, "checks": []}
    holder = Path(tempfile.mkdtemp(prefix="tender-patch-"))
    started = perf_counter()
    try:
        root = repository.materialize(base, holder / "repo")
        head = run_safe_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()
        clean = not run_safe_git(["status", "--porcelain"], cwd=root).stdout.strip()
        group["pristine"] = head == base and clean
        if patch_text is not None:
            try:
                run_safe_git(["apply", "--check", "-"], cwd=root, input_text=patch_text)
                run_safe_git(["apply", "-"], cwd=root, input_text=patch_text)
            except RepositoryError as exc:
                group.update(status="error", apply_error=str(exc)[:500])
                return group
            restored, removed = restore_evaluator_paths(root, base, changed, criteria.integrity)
            group["restored_from_base"] = list(restored)
            group["removed_patch_additions"] = list(removed)
        if assets is not None:
            assets.install(root)
        sandbox = create_sandbox(root, backend, criteria.constraints.timeout_seconds)
        try:
            for command in criteria.setup:
                result = sandbox.run(command)
                group["checks"].append(_command_record("setup", command, result))
                if not result.passed:
                    group["status"] = "error"
                    group["environment_failed"] = True
                    group["note"] = "A setup command failed; checks did not run."
                    return group
            failed = timed_out = False
            for spec in checks:
                nonce = uuid.uuid4().hex[:12]
                command, report = _check_command(spec, python, nonce)
                result = sandbox.run(command)
                record = _command_record(
                    spec.name, spec.command or " ".join(spec.pytest or []), result
                )
                record["executed"] = command
                if report is not None:
                    tests, stdout = _parse_junit(result.stdout, spec.pytest or [])
                    record["tests"] = tests
                    record["stdout"] = sanitize_text(stdout[-4000:])
                    missing = [test["id"] for test in tests if test["outcome"] == "missing"]
                    if result.passed and (missing or not tests):
                        record["passed"] = False
                        record["detail"] = "exit status 0 but the JUnit report does not list " + (
                            ", ".join(missing) if missing else "the requested tests"
                        )
                    elif any(test["outcome"] != "passed" for test in tests):
                        record["passed"] = False
                group["checks"].append(record)
                timed_out = timed_out or result.timed_out
                failed = failed or not record["passed"]
            group["status"] = "error" if timed_out else ("failed" if failed else "passed")
            group["timed_out"] = timed_out
        finally:
            sandbox.destroy()
    finally:
        shutil.rmtree(holder, ignore_errors=True)
        group["duration_ms"] = round((perf_counter() - started) * 1000)
    return group


def _command_record(name: str, command: str, result: CommandResult) -> dict[str, Any]:
    return {
        "name": name,
        "command": command,
        "passed": result.passed,
        "return_code": result.return_code,
        "timed_out": result.timed_out,
        "duration_ms": result.duration_ms,
        "stdout": sanitize_text(result.stdout[-4000:]),
        "stderr": sanitize_text(result.stderr[-4000:]),
    }


def docker_available() -> bool:
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


# -------------------------------------------------------------------- report


class PatchEvaluationError(RuntimeError):
    """The evaluation could not start (bad inputs or refused configuration)."""


def evaluate_patch(
    repo: str | Path,
    base: str,
    patch: str | Path,
    criteria_path: str | Path,
    out_dir: str | Path,
    *,
    sandbox: Literal["docker", "local"] = "docker",
    trust_local_execution: bool = False,
    python: str | None = None,
    repository_name: str | None = None,
) -> dict[str, Any]:
    """Evaluate one patch and write report.json, report.md, and evidence/ to out_dir."""
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise PatchEvaluationError(f"output directory is not empty: {out}")
    if sandbox == "local" and not trust_local_execution:
        raise PatchEvaluationError(
            "the local backend runs the patch on this machine without isolation; use "
            "--sandbox docker, or pass --trust-local-execution for a patch you wrote or trust"
        )
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = perf_counter()
    timing: dict[str, int] = {}
    criteria, criteria_raw = AcceptanceCriteria.load(criteria_path)
    patch_raw = Path(patch).read_bytes()
    patch_text = patch_raw.decode("utf-8", errors="replace")
    interpreter = python or criteria.constraints.python
    assets = None
    if criteria.integrity.hidden_tests:
        assets_dir = (Path(criteria_path).parent / criteria.integrity.hidden_tests).resolve()
        repo_root = Path(repo).expanduser().resolve()
        if assets_dir == repo_root or repo_root in assets_dir.parents:
            raise PatchEvaluationError("hidden evaluator tests must live outside the repository")
        assets = EvaluatorAssets.from_directory(assets_dir)
    environment = environment_identity(sandbox)
    environment["python_command"] = interpreter
    environment["trust_local_execution"] = trust_local_execution if sandbox == "local" else None
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "report_id": str(uuid.uuid4()),
        "created_at": created,
        "task": criteria.task.model_dump(),
        "criteria": {
            "sha256": hashlib.sha256(criteria_raw).hexdigest(),
            "file": Path(criteria_path).name,
            "target": [spec.model_dump(exclude_none=True) for spec in criteria.target],
            "regression": [spec.model_dump(exclude_none=True) for spec in criteria.regression],
            "setup": criteria.setup,
            "constraints": criteria.constraints.model_dump(),
        },
        "patch": {
            "file": Path(patch).name,
            "sha256": hashlib.sha256(patch_raw).hexdigest(),
            "bytes": len(patch_raw),
            "diff": patch_text,
        },
        "environment": environment,
        "explanations": [],
        "unknowns": [],
    }
    outcome = EvaluationOutcome(provider="not_used")
    budget = ExecutionBudget(criteria.constraints.deadline_seconds)
    token = current_budget.set(budget)
    try:
        with TrustedRepository(repo) as repository:
            before = repository.identity()
            commit = repository.resolve_commit(base)
            report["repository"] = {
                "name": repository_name or repository.source.name,
                "requested_base": base,
                "base_commit": commit,
                "head_commit": before.head_commit,
                "working_tree_dirty": before.dirty,
                "working_tree_entries": len(before.status_entries),
                "note": "Uncommitted and ignored files are never used; workspaces are built "
                "from Git objects at base_commit.",
            }
            outcome.verification_commit = commit
            stage = perf_counter()
            validation = validate_patch(patch_raw, criteria, assets)
            changed = sorted(
                {
                    path
                    for item in validation.files
                    for path in (item.old_path, item.new_path)
                    if path
                }
            )
            if validation.passed:
                try:
                    probe = repository.materialize(
                        commit, Path(tempfile.mkdtemp(prefix="tender-patch-")) / "repo"
                    )
                    try:
                        check = run_safe_git(
                            ["apply", "--check", "-"], cwd=probe, input_text=patch_text, check=False
                        )
                    finally:
                        shutil.rmtree(probe.parent, ignore_errors=True)
                    validation.add(
                        "applies cleanly to the base commit",
                        check.returncode == 0,
                        (check.stderr or "").strip()[:500],
                    )
                except RepositoryError as exc:
                    validation.add("applies cleanly to the base commit", False, str(exc)[:500])
            timing["validation"] = round((perf_counter() - stage) * 1000)
            files = validation.files
            report["patch"].update(
                files=[item.as_dict() for item in files],
                additions=sum(item.additions for item in files),
                deletions=sum(item.deletions for item in files),
            )
            report["validation"] = {"passed": validation.passed, "checks": validation.checks}
            findings = check_patch_paths(changed, criteria.integrity, assets)
            integrity = {
                "passed": not findings,
                "violations": [item.as_dict() for item in findings],
                "policy": criteria.integrity.model_dump(exclude={"hidden_tests"}),
                "evaluator_assets": list(assets.manifest()) if assets else [],
                "restored_from_base": [],
                "removed_patch_additions": [],
            }
            report["integrity"] = integrity
            outcome.integrity = "passed" if not findings else "violated"
            if not validation.passed:
                outcome.policy = "rejected"
                outcome.target = CheckOutcome.NOT_RUN
                outcome.regression = CheckOutcome.NOT_RUN
                failures = [check for check in validation.checks if not check["passed"]]
                report["explanations"].extend(
                    f"Validation failed: {check['name']}"
                    + (f" ({check['detail']})" if check["detail"] else "")
                    for check in failures
                )
            else:
                backend = sandbox
                if backend == "docker" and not docker_available():
                    raise SandboxUnavailable("Docker daemon is not reachable; refusing to run")
                if criteria.constraints.check_baseline:
                    stage = perf_counter()
                    baseline = _run_group(
                        "baseline",
                        repository,
                        commit,
                        None,
                        [],
                        criteria.target,
                        criteria,
                        assets,
                        backend,
                        interpreter,
                    )
                    timing["baseline"] = round((perf_counter() - stage) * 1000)
                    baseline["note"] = (
                        "Target checks failed before the patch, so they can distinguish a fix."
                        if baseline.get("status") == "failed"
                        else "Target checks already pass without the patch; they do not show "
                        "that the patch is needed."
                        if baseline.get("status") == "passed"
                        else "Baseline could not be established."
                    )
                    report["baseline"] = baseline
                    if baseline.get("status") == "passed":
                        report["unknowns"].append(
                            "Whether the patch is needed: target checks pass on the base commit."
                        )
                stage = perf_counter()
                target = _run_group(
                    "target",
                    repository,
                    commit,
                    patch_text,
                    changed,
                    criteria.target,
                    criteria,
                    assets,
                    backend,
                    interpreter,
                )
                timing["target_verification"] = round((perf_counter() - stage) * 1000)
                report["target"] = target
                integrity["restored_from_base"] = target.get("restored_from_base", [])
                integrity["removed_patch_additions"] = target.get("removed_patch_additions", [])
                outcome.target = _group_outcome(target)
                outcome.independently_verified = bool(
                    target.get("pristine") and target.get("status") == "passed"
                )
                if criteria.regression:
                    stage = perf_counter()
                    regression = _run_group(
                        "regression",
                        repository,
                        commit,
                        patch_text,
                        changed,
                        criteria.regression,
                        criteria,
                        assets,
                        backend,
                        interpreter,
                    )
                    timing["regression_verification"] = round((perf_counter() - stage) * 1000)
                    report["regression"] = regression
                    outcome.regression = _group_outcome(regression)
                else:
                    report["regression"] = {
                        "kind": "regression",
                        "status": "not_configured",
                        "checks": [],
                    }
                    outcome.regression = CheckOutcome.NOT_CONFIGURED
                    report["unknowns"].append(
                        "Regression preservation: no regression checks were configured."
                    )
                outcome.environment = "ok"
                for group in (report.get("baseline"), target, report.get("regression")):
                    if group and group.get("environment_failed"):
                        outcome.environment = "setup_failed"
                    if group and group.get("timed_out"):
                        outcome.timed_out = True
                report["explanations"].extend(_explain(report))
            after = repository.identity()
            report["repository"]["source_unchanged"] = (
                after.head_commit == before.head_commit
                and after.status_digest == before.status_digest
            )
    except SandboxUnavailable as exc:
        outcome.environment = "sandbox_unavailable"
        report["explanations"].append(f"Sandbox unavailable: {exc}. Nothing was executed.")
    except RepositoryBoundaryError as exc:
        outcome.policy = "rejected"
        report.setdefault(
            "repository", {"name": repository_name or Path(repo).name, "requested_base": base}
        )
        report["explanations"].append(f"Repository rejected: {exc}")
    finally:
        current_budget.reset(token)
    if budget.exhausted():
        outcome.timed_out = True
        report["explanations"].append("The evaluation deadline was exhausted.")
    outcome.finalize()
    report["outcome"] = outcome.model_dump(mode="json")
    timing["total_wall_clock"] = round((perf_counter() - started) * 1000)
    report["timing_ms"] = timing
    report["unknowns"].append(
        "Behaviour not exercised by the configured checks. Passing checks do not prove the "
        "absence of other defects or regressions."
    )
    report["summary"] = _summary(report)
    report = sanitize(report)
    report["patch"]["diff"] = patch_text  # keep the exact bytes the digest refers to
    _write_outputs(out, report, patch_raw, criteria_raw)
    return report


def _group_outcome(group: dict[str, Any]) -> CheckOutcome:
    status = group.get("status")
    if status == "passed":
        return CheckOutcome.PASSED
    if status == "failed":
        return CheckOutcome.FAILED
    return CheckOutcome.ERROR


def _explain(report: dict[str, Any]) -> list[str]:
    lines = []
    for key, label in (("target", "Target"), ("regression", "Regression")):
        group = report.get(key) or {}
        if group.get("apply_error"):
            lines.append(f"{label} workspace: the patch did not apply ({group['apply_error']}).")
        for check in group.get("checks", []):
            if check.get("name") == "setup" and not check.get("passed"):
                lines.append(f"{label} setup failed: {check['command']}")
                continue
            if check.get("passed"):
                continue
            failing = [
                test.get("failing_cases") or test["id"]
                for test in check.get("tests", [])
                if test["outcome"] != "passed"
            ]
            reason = check.get("detail") or (
                f"failing: {', '.join(failing)}" if failing else f"exit {check.get('return_code')}"
            )
            lines.append(f"{label} check “{check['name']}” failed ({reason}).")
    restored = report.get("integrity", {}).get("restored_from_base", []) + report.get(
        "integrity", {}
    ).get("removed_patch_additions", [])
    if restored:
        lines.append(
            "Evaluator-owned files changed by the patch were reset to the base commit for "
            f"verification: {', '.join(restored)}."
        )
    return lines


def _summary(report: dict[str, Any]) -> str:
    outcome = report["outcome"]
    overall = outcome["overall"]
    if overall == "accepted":
        regression = (
            "regression checks also passed"
            if outcome["regression"] == "passed"
            else "regression preservation was not measured"
        )
        return (
            f"Accepted: target checks passed in a fresh workspace at "
            f"{str(outcome.get('verification_commit'))[:10]} and {regression}."
        )
    if overall == "rejected":
        if outcome["policy"] == "rejected":
            return "Rejected before execution: the patch failed validation."
        if outcome["integrity"] == "violated":
            return "Rejected: the patch touches evaluator-owned or protected files."
        failed = [name for name in ("target", "regression") if outcome[name] == "failed"]
        return f"Rejected: {' and '.join(failed)} checks failed with the patch applied."
    if overall == "error":
        return "Not evaluated: the environment could not run the checks."
    return "Inconclusive: the checks did not finish within their limits."


def _write_outputs(
    out: Path, report: dict[str, Any], patch_raw: bytes, criteria_raw: bytes
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    bundle = EvidenceWriter(out / "evidence")
    bundle.add_bytes("source/patch.diff", patch_raw, kind="source")
    bundle.add_bytes("source/criteria", criteria_raw, kind="source")
    bundle.add_json("source/repository.json", report.get("repository", {}), kind="source")
    bundle.add_json("source/environment.json", report["environment"], kind="source")
    bundle.add_json("source/validation.json", report.get("validation", {}), kind="source")
    for key in ("baseline", "target", "regression"):
        if key in report:
            bundle.add_json(f"source/checks/{key}.json", report[key], kind="source")
    bundle.add_json("source/integrity.json", report.get("integrity", {}), kind="source")
    bundle.add_json(
        "derived/outcome.json",
        {
            "outcome": report["outcome"],
            "summary": report["summary"],
            "timing_ms": report["timing_ms"],
        },
        kind="derived",
    )
    manifest = bundle.finish(
        {
            "report_id": report["report_id"],
            "generator": "tender.patch_eval",
            "patch_sha256": report["patch"]["sha256"],
            "base_commit": report.get("repository", {}).get("base_commit"),
        }
    )
    manifest_bytes = (out / "evidence" / "manifest.json").read_bytes()
    report["evidence"] = {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "files": manifest["files"],
        "signed": False,
    }
    report["markdown"] = render_markdown(report)
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (out / "report.md").write_text(report["markdown"], encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    """A code-review friendly summary; long outputs stay in report.json and evidence/."""
    outcome = report["outcome"]
    repository = report.get("repository", {})
    backend = report["environment"]["sandbox"]["backend"]
    lines = [
        f"# Tender patch verification: {report['task']['title']}",
        "",
        f"**{outcome['overall'].upper()}** — {report['summary']}",
        "",
        "| Property | Result |",
        "| --- | --- |",
        f"| Repository | `{repository.get('name', '')}` |",
        f"| Base commit | `{repository.get('base_commit', 'unknown')}` |",
        f"| Patch | `{report['patch']['file']}` · sha256 `{report['patch']['sha256'][:16]}` |",
        f"| Validation | {'passed' if report.get('validation', {}).get('passed') else 'failed'} |",
        f"| Integrity | {outcome['integrity']} |",
        f"| Baseline | {report.get('baseline', {}).get('status', 'not run')} |",
        f"| Target checks | {outcome['target']} |",
        f"| Regression checks | {outcome['regression']} |",
        f"| Environment | {outcome['environment']} ({backend}) |",
        f"| Independently verified | {'yes' if outcome['independently_verified'] else 'no'} |",
        f"| Source repository unchanged | {repository.get('source_unchanged', 'unknown')} |",
        f"| Duration | {report['timing_ms'].get('total_wall_clock', 0) / 1000:.1f} s |",
        "",
    ]
    if report.get("explanations"):
        lines += ["## Why", ""] + [f"- {line}" for line in report["explanations"]] + [""]
    for key in ("baseline", "target", "regression"):
        group = report.get(key)
        if not group or not group.get("checks"):
            continue
        lines += [f"## {key.capitalize()} checks ({group.get('status')})", ""]
        for check in group["checks"]:
            mark = "x" if check.get("passed") else " "
            lines.append(f"- [{mark}] {check['name']} — exit {check.get('return_code')}")
            for test in check.get("tests", []):
                detail = f" ({test['failing_cases']})" if test.get("failing_cases") else ""
                lines.append(f"  - `{test['id']}`: {test['outcome']}{detail}")
        lines.append("")
    lines += ["## Not established", ""] + [f"- {item}" for item in report.get("unknowns", [])]
    lines += [
        "",
        f"Evidence manifest sha256 `{report.get('evidence', {}).get('manifest_sha256', '')}` "
        "(hashed, not signed). Verify with `tender evidence verify <dir>/evidence`.",
        "",
    ]
    return "\n".join(lines)


def load_reports(root: str | Path, *, limit: int = 500) -> list[dict[str, Any]]:
    """Read report.json files under root (newest first); unreadable files are skipped."""
    base = Path(root)
    if not base.is_dir():
        return []
    reports = []
    for path in base.rglob("report.json"):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(report, dict) and report.get("schema") == REPORT_SCHEMA:
            reports.append(report)
    reports.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return reports[:limit]
