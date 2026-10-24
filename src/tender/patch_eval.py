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

import ast
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
    EvaluatorConfigError,
    IntegrityPolicy,
    check_patch_paths,
    matches,
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
COLLECT_MARKER = "----TENDER-COLLECTED-TESTS----"
_NODE_ID = re.compile(r"^[A-Za-z0-9_./\-]+(?:::[A-Za-z0-9_\[\]\-.,=: ]+)?$")


# ------------------------------------------------------------------ criteria


class CheckSpec(BaseModel):
    """One evaluator-owned check: a pytest selection (preferred) or a command."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    pytest: list[str] | None = Field(default=None, max_length=200)
    command: str | None = Field(default=None, min_length=1, max_length=2000)
    #: Exact test IDs the evaluator requires; overrides automatic coverage discovery.
    expected_tests: list[str] | None = Field(default=None, max_length=5000)
    #: "base" (default): required coverage comes from evaluator-controlled sources.
    #: "candidate": the evaluator explicitly accepts the candidate's own collection.
    coverage: Literal["base", "candidate"] = "base"

    @model_validator(mode="after")
    def _one_kind(self) -> CheckSpec:
        if (self.pytest is None) == (self.command is None):
            raise ValueError(f"check {self.name!r} needs exactly one of 'pytest' or 'command'")
        for node in [*(self.pytest or []), *(self.expected_tests or [])]:
            if not _NODE_ID.fullmatch(node) or ".." in node.split("::")[0].split("/"):
                raise ValueError(f"check {self.name!r} has an invalid pytest selector: {node!r}")
        selectors = self.pytest or []
        for index, first in enumerate(selectors):
            for second in selectors[index + 1 :]:
                if _selector_matches(first, second) or _selector_matches(second, first):
                    raise ValueError(
                        f"check {self.name!r} has overlapping selectors {first!r} and {second!r}"
                    )
        if self.expected_tests is not None:
            if self.pytest is None:
                raise ValueError(f"check {self.name!r}: expected_tests needs pytest selectors")
            if len(set(self.expected_tests)) != len(self.expected_tests):
                raise ValueError(f"check {self.name!r} lists duplicate expected_tests")
            for node in self.expected_tests:
                if not any(_selector_matches(node, selector) for selector in selectors):
                    raise ValueError(
                        f"check {self.name!r}: expected test {node!r} is outside its selectors"
                    )
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
    """Shell command for one check.

    Structured pytest checks first collect the selected test IDs (the expected
    set), then run them with a JUnit report; both are echoed after markers so
    they survive the sandbox boundary.
    """
    if spec.command is not None:
        return spec.command, None
    report = f".tender-junit-{nonce}.xml"
    collected = f".tender-collect-{nonce}.txt"
    selectors = " ".join(_shell_quote(node) for node in spec.pytest or [])
    common = f"{python} -m pytest -p no:cacheprovider -o addopts="
    command = (
        f"{common} --collect-only -q {selectors} > {collected} 2>&1; collect_status=$?; "
        f"{common} -q -o junit_family=xunit1 --junitxml={report} {selectors}; status=$?; "
        f"echo '{COLLECT_MARKER}'; echo \"$collect_status\"; cat {collected} 2>/dev/null; "
        f"echo '{JUNIT_MARKER}'; cat {report} 2>/dev/null; exit $status"
    )
    return command, report


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


_COLLECTED_ID = re.compile(r"^\S[^\n]*?\.py::\S.*$")


def _selector_matches(node_id: str, selector: str) -> bool:
    """Pytest selection semantics: file, class, function, or exact parametrized ID."""
    return node_id == selector or node_id.startswith((selector + "::", selector + "["))


def _int_attribute(element: ElementTree.Element, name: str) -> int | None:
    value = element.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return -1


def junit_node_ids(xml_text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Rebuild full pytest node IDs from an xunit1 JUnit report and check its totals.

    pytest writes ``classname`` as the dotted module path plus any class names
    and ``name`` as the function name with its parameter ID; ``file`` is the
    test file. Each suite's declared ``tests``, ``failures``, ``errors``, and
    ``skipped`` totals must equal the test cases it actually contains, so a
    report cannot declare results it does not show. Returns
    ((node_id, status) ..., problems).
    """
    problems: list[str] = []
    cases: list[tuple[str, str]] = []
    try:
        root = ElementTree.fromstring(xml_text.strip())
    except ElementTree.ParseError:
        return [], ["JUnit report is not valid XML"]
    if root.tag not in {"testsuites", "testsuite"}:
        return [], ["JUnit report has an unexpected root element"]
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag == "testsuites" and root.findall(".//testsuite//testsuite"):
        problems.append("JUnit report nests test suites, which Tender does not accept")
    if root.tag == "testsuites" and root.findall("testcase"):
        problems.append("JUnit report has test cases outside any test suite")
    grand = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        counted = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for case in suite.findall("testcase"):
            counted["tests"] += 1
            failure = case.find("failure") is not None
            error = case.find("error") is not None
            skipped = case.find("skipped") is not None
            counted["failures"] += failure
            counted["errors"] += error
            counted["skipped"] += skipped
            file = case.get("file") or ""
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            if not file.endswith(".py") or not name:
                problems.append(f"test case without file or name: {classname}.{name}")
                continue
            module = file[:-3].replace("/", ".")
            if classname == module:
                classes: list[str] = []
            elif classname.startswith(module + "."):
                classes = classname[len(module) + 1 :].split(".")
            else:
                problems.append(f"test case {classname}::{name} does not belong to {file}")
                continue
            status = "failed" if failure or error else "skipped" if skipped else "passed"
            cases.append(("::".join([file, *classes, name]), status))
        for key, value in counted.items():
            declared = _int_attribute(suite, key)
            if declared is None:
                if key == "tests":
                    problems.append("JUnit test suite does not declare its test count")
                continue
            if declared != value:
                problems.append(
                    f"JUnit suite declares {key}={declared} but contains {value} matching records"
                )
            grand[key] += value
    if root.tag == "testsuites":
        for key, value in grand.items():
            declared = _int_attribute(root, key)
            if declared is not None and declared != value:
                problems.append(
                    f"JUnit report declares {key}={declared} but its suites contain {value}"
                )
    return cases, problems


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def reconcile_pytest_results(
    selectors: list[str],
    collected_ids: list[str] | None,
    collect_status: int | None,
    junit_xml: str | None,
    *,
    required_ids: list[str] | None,
) -> tuple[list[dict[str, str]], list[str], dict[str, Any]]:
    """Check the trusted required tests (and any extra candidate cases) all passed.

    ``required_ids`` comes from an evaluator-controlled source (see
    ``establish_expected_coverage``), never from the candidate workspace, so a
    candidate cannot shrink what must run. ``collected_ids`` is the candidate
    workspace's own collection: it is evidence, and any extra cases it adds must
    pass too. Every expected test must be executed exactly once and pass.
    Returns per-selector outcomes, check-level problems, and a coverage record.
    """
    problems: list[str] = []
    if required_ids is None:
        problems.append("trusted expected coverage is unavailable for this check")
        required_ids = []
    duplicate_required = _duplicates(required_ids)
    if duplicate_required:
        problems.append(
            "expected coverage lists duplicate test IDs: " + ", ".join(duplicate_required[:10])
        )
    if collected_ids is None:
        problems.append("test collection output is missing")
        collected_ids = []
    elif collect_status != 0:
        problems.append(f"test collection failed (exit {collect_status})")
    duplicate_collected = _duplicates(collected_ids)
    if duplicate_collected:
        problems.append(
            "test collection produced duplicate test IDs: " + ", ".join(duplicate_collected[:10])
        )
    cases: list[tuple[str, str]] = []
    if junit_xml is None or not junit_xml.strip():
        problems.append("no JUnit report was produced (the test process may have exited early)")
    else:
        cases, junit_problems = junit_node_ids(junit_xml)
        problems.extend(junit_problems)
        if not cases and not junit_problems:
            problems.append("JUnit report contains no test cases")
    results: dict[str, list[str]] = {}
    for node_id, status in cases:
        results.setdefault(node_id, []).append(status)
    required = set(required_ids)
    collected = set(collected_ids)
    expected_all: set[str] = set()
    outcomes: list[dict[str, str]] = []
    for selector in selectors:
        required_here = {node for node in required if _selector_matches(node, selector)}
        expected = sorted(
            required_here | {node for node in collected if _selector_matches(node, selector)}
        )
        expected_all.update(expected)
        found = {node: results.get(node, []) for node in expected}
        missing = [node for node, seen in found.items() if not seen]
        duplicated = [node for node, seen in found.items() if len(seen) > 1]
        failed = [node for node, seen in found.items() if seen == ["failed"]]
        skipped = [node for node, seen in found.items() if seen == ["skipped"]]
        if not required_here or missing:
            outcome = "missing"
        elif duplicated:
            outcome = "ambiguous"
        elif failed:
            outcome = "failed"
        elif skipped:
            outcome = "skipped"
        else:
            outcome = "passed"
        entry = {"id": selector, "outcome": outcome, "cases": str(len(expected))}
        not_passed = missing + duplicated + failed + skipped
        if not_passed and expected != [selector]:
            entry["failing_cases"] = ", ".join(not_passed[:20])
        outcomes.append(entry)
    unexpected = sorted(set(results) - expected_all)
    if unexpected:
        problems.append(
            "JUnit report lists tests that were not selected: " + ", ".join(unexpected[:10])
        )
    not_collected = sorted(required - collected)
    if not_collected:
        problems.append(
            "the candidate workspace no longer collects required tests: "
            + ", ".join(not_collected[:10])
        )
    executed = sorted(results)
    coverage = {
        "expected": sorted(required),
        "collected": list(collected_ids),
        "executed": executed,
        "missing_required": sorted(node for node in required if not results.get(node)),
        "not_collected_by_candidate": not_collected,
        "extra_candidate_cases": sorted(collected - required),
        "duplicate_collected": duplicate_collected,
    }
    return outcomes, problems, coverage


def _parse_check_output(
    stdout: str, selectors: list[str], required_ids: list[str] | None
) -> tuple[list[dict[str, str]], list[str], dict[str, Any], str]:
    """Split sandbox output into (outcomes, problems, coverage, readable test output)."""
    before, collect_marker, rest = stdout.partition(COLLECT_MARKER)
    collected_text, junit_marker, junit_xml = rest.partition(JUNIT_MARKER)
    collected_ids, collect_status = (
        _parse_collection(collected_text) if collect_marker else (None, None)
    )
    outcomes, problems, coverage = reconcile_pytest_results(
        selectors,
        collected_ids,
        collect_status,
        junit_xml if junit_marker else None,
        required_ids=required_ids,
    )
    return outcomes, problems, coverage, before


def _parse_collection(text: str) -> tuple[list[str], int | None]:
    """Collected node IDs in order (duplicates kept) and the collection exit status."""
    lines = text.strip("\n").splitlines()
    status: int | None = None
    if lines and lines[0].strip().lstrip("-").isdigit():
        status = int(lines[0].strip())
        lines = lines[1:]
    return [line.rstrip() for line in lines if _COLLECTED_ID.match(line.rstrip())], status


# ------------------------------------------------- trusted expected coverage

#: Source text that makes static test enumeration unsafe: test IDs could then
#: depend on runtime values (parametrization, fixture params, collection hooks).
_DYNAMIC_COLLECTION = re.compile(
    r"parametrize|params\s*=|pytest_generate_tests|pytest_collection_modifyitems|"
    r"pytest_pycollect|pytest_collect_file|importorskip|pytestmark|collect_ignore|"
    r"__test__|load_tests"
)
_COLLECTION_SETTINGS = re.compile(r"python_(files|classes|functions)\s*[=:]")


def _static_test_ids(
    root: Path,
    selectors: list[str],
    criteria: AcceptanceCriteria,
    assets: EvaluatorAssets | None,
) -> tuple[list[str] | None, str]:
    """Enumerate test IDs from evaluator-owned source without importing anything.

    Used only when the base commit cannot be collected (for example, a target
    test for a function that does not exist yet). It refuses whenever test
    identity could depend on runtime values or on collection customization.
    """
    owned_assets = set(assets.paths) if assets else set()
    for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "pytest.toml"):
        config = root / name
        if config.is_file() and _COLLECTION_SETTINGS.search(config.read_text(errors="replace")):
            return None, f"{name} customizes test discovery"
    for conftest in root.rglob("conftest.py"):
        if ".git" in conftest.parts:
            continue
        if _DYNAMIC_COLLECTION.search(conftest.read_text(errors="replace")):
            relative = conftest.relative_to(root).as_posix()
            return None, f"{relative} can change which tests are collected"
    ids: list[str] = []
    for selector in selectors:
        file = selector.split("::", 1)[0]
        if not (matches(file, criteria.integrity.restored_paths) or file in owned_assets):
            return None, f"{file} is not evaluator-owned, so its source is not trusted"
        path = root / file
        if not path.is_file():
            return None, f"{file} does not exist in the evaluator workspace"
        source = path.read_text(encoding="utf-8", errors="replace")
        if _DYNAMIC_COLLECTION.search(source):
            return None, f"{file} uses dynamic parametrization or collection"
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None, f"{file} does not parse"
        found = _ast_tests(tree.body, [file])
        if found is None:
            return None, f"{file} uses test-class inheritance, which static enumeration skips"
        selected = [node for node in found if _selector_matches(node, selector)]
        if not selected:
            return None, f"no tests in {file} match {selector}"
        ids.extend(selected)
    return ids, "enumerated from evaluator-owned test source"


def _ast_tests(body: list[ast.stmt], prefix: list[str]) -> list[str] | None:
    names: dict[str, None] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            names["::".join([*prefix, node.name])] = None
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if node.bases or any(
                isinstance(item, ast.FunctionDef) and item.name == "__init__" for item in node.body
            ):
                return None
            nested = _ast_tests(node.body, [*prefix, node.name])
            if nested is None:
                return None
            names.update(dict.fromkeys(nested))
    return list(names)


def _collect_command(selectors: list[str], python: str) -> str:
    quoted = " ".join(_shell_quote(node) for node in selectors)
    return (
        f"{python} -m pytest -p no:cacheprovider -o addopts= --collect-only -q {quoted} "
        f"> .tender-collect.txt 2>&1; status=$?; echo '{COLLECT_MARKER}'; echo \"$status\"; "
        "cat .tender-collect.txt; exit 0"
    )


def establish_expected_coverage(
    repository: TrustedRepository,
    base: str,
    criteria: AcceptanceCriteria,
    assets: EvaluatorAssets | None,
    backend: str,
    python: str,
) -> dict[str, dict[str, Any]]:
    """Required test IDs per structured check, from candidate-independent sources.

    Precedence: evaluator-declared ``expected_tests``; collection in a fresh
    workspace at the base commit with evaluator assets and no patch; static
    enumeration of evaluator-owned test source; the candidate's own collection
    only when the evaluator set ``coverage = "candidate"``. Otherwise the check
    has no trusted coverage and fails closed.
    """
    checks = [
        (f"{kind}[{index}]", spec)
        for kind, specs in (("target", criteria.target), ("regression", criteria.regression))
        for index, spec in enumerate(specs)
        if spec.pytest is not None
    ]
    coverage: dict[str, dict[str, Any]] = {}
    if not checks:
        return coverage
    holder = Path(tempfile.mkdtemp(prefix="tender-patch-"))
    try:
        root = repository.materialize(base, holder / "repo")
        if assets is not None:
            assets.install(root)
        sandbox = create_sandbox(root, backend, criteria.constraints.timeout_seconds)
        try:
            setup_error = None
            for command in criteria.setup:
                if not sandbox.run(command).passed:
                    setup_error = f"setup command failed at the base commit: {command}"
                    break
            for key, spec in checks:
                selectors = spec.pytest or []
                entry: dict[str, Any] = {"check": spec.name, "selectors": selectors}
                if setup_error is None:
                    result = sandbox.run(_collect_command(selectors, python))
                    marker = COLLECT_MARKER in result.stdout
                    ids, status = (
                        _parse_collection(result.stdout.partition(COLLECT_MARKER)[2])
                        if marker
                        else ([], None)
                    )
                    entry["base_collection"] = {
                        "status": status,
                        "ids": ids,
                        "duplicates": _duplicates(ids),
                    }
                    base_ok = status == 0 and ids and not _duplicates(ids)
                else:
                    entry["base_collection"] = {"status": None, "ids": [], "error": setup_error}
                    base_ok = False
                if spec.expected_tests is not None:
                    entry.update(
                        provenance="evaluator_declared", expected=list(spec.expected_tests)
                    )
                elif base_ok:
                    entry.update(provenance="base_commit_collection", expected=ids)
                else:
                    static, reason = _static_test_ids(root, selectors, criteria, assets)
                    if static is not None:
                        entry.update(provenance="evaluator_source_static", expected=static)
                        entry["note"] = reason
                    elif spec.coverage == "candidate":
                        entry.update(provenance="candidate_collection_authorized", expected=None)
                        entry["note"] = (
                            "the evaluator explicitly accepts the candidate's collection"
                        )
                    else:
                        reasons = [
                            setup_error
                            or (
                                "the base commit cannot be collected"
                                f" (exit {entry['base_collection']['status']})"
                                if not ids or status != 0
                                else "the base collection lists duplicate test IDs"
                            ),
                            f"static enumeration refused: {reason}",
                        ]
                        entry.update(
                            provenance="unavailable",
                            expected=None,
                            reason="; ".join(reasons)
                            + ". Declare expected_tests for this check, or set "
                            'coverage = "candidate" to accept the candidate\'s collection.',
                        )
                coverage[key] = entry
        finally:
            sandbox.destroy()
    finally:
        shutil.rmtree(holder, ignore_errors=True)
    return coverage


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
    coverage: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Run one check group in a brand-new workspace; the workspace is then deleted.

    ``coverage`` gives each check's trusted required test IDs (aligned with
    ``checks``), established before any candidate code ran.
    """
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
            try:
                restored, removed, sections = restore_evaluator_paths(
                    root, base, changed, criteria.integrity
                )
            except EvaluatorConfigError as exc:
                group.update(status="error", integrity_error=str(exc)[:500])
                return group
            group["restored_from_base"] = list(restored)
            group["removed_patch_additions"] = list(removed)
            group["evaluator_sections"] = list(sections)
            group["tested_as_submitted"] = not (
                restored
                or removed
                or any(item["action"] == "restored_section" for item in sections)
            )
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
            failed = timed_out = unavailable = False
            for index, spec in enumerate(checks):
                trusted = coverage[index] if coverage else None
                nonce = uuid.uuid4().hex[:12]
                command, report = _check_command(spec, python, nonce)
                result = sandbox.run(command)
                record = _command_record(
                    spec.name, spec.command or " ".join(spec.pytest or []), result
                )
                record["executed"] = command
                if report is not None:
                    provenance = trusted.get("provenance") if trusted else "unavailable"
                    required = trusted.get("expected") if trusted else None
                    if provenance == "candidate_collection_authorized":
                        collected, _ = _parse_collection(
                            result.stdout.partition(COLLECT_MARKER)[2].partition(JUNIT_MARKER)[0]
                        )
                        required = list(dict.fromkeys(collected))
                    tests, problems, check_coverage, stdout = _parse_check_output(
                        result.stdout, spec.pytest or [], required
                    )
                    check_coverage["provenance"] = provenance
                    if trusted and trusted.get("reason"):
                        check_coverage["unavailable_reason"] = trusted["reason"]
                        record["coverage_unavailable"] = True
                        unavailable = True
                    record["coverage"] = check_coverage
                    record["tests"] = tests
                    record["stdout"] = sanitize_text(stdout[-4000:])
                    if problems:
                        record["report_problems"] = problems[:10]
                    not_passed = [test for test in tests if test["outcome"] != "passed"]
                    if problems or not_passed:
                        record["passed"] = False
                    if result.passed and (problems or not_passed):
                        record["detail"] = "exit status 0, but " + (
                            problems[0]
                            if problems
                            else "the results do not show every expected test passing: "
                            + ", ".join(test["id"] for test in not_passed)
                        )
                group["checks"].append(record)
                timed_out = timed_out or result.timed_out
                failed = failed or not record["passed"]
            real_failure = any(
                not check.get("passed") and not check.get("coverage_unavailable")
                for check in group["checks"]
            )
            if timed_out:
                group["status"] = "error"
            elif real_failure:
                group["status"] = "failed"
            elif unavailable:
                # Fail closed: without trusted coverage a pass cannot be claimed.
                group["status"] = "error"
                group["note"] = "Trusted expected test coverage could not be established."
            else:
                group["status"] = "failed" if failed else "passed"
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
                stage = perf_counter()
                expected = establish_expected_coverage(
                    repository, commit, criteria, assets, backend, interpreter
                )
                timing["expected_coverage"] = round((perf_counter() - stage) * 1000)
                report["coverage"] = expected
                target_coverage = [
                    expected.get(f"target[{index}]") for index in range(len(criteria.target))
                ]
                regression_coverage = [
                    expected.get(f"regression[{index}]")
                    for index in range(len(criteria.regression))
                ]
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
                        target_coverage,
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
                    target_coverage,
                )
                timing["target_verification"] = round((perf_counter() - stage) * 1000)
                report["target"] = target
                integrity["restored_from_base"] = target.get("restored_from_base", [])
                integrity["removed_patch_additions"] = target.get("removed_patch_additions", [])
                integrity["evaluator_sections"] = target.get("evaluator_sections", [])
                integrity["tested_as_submitted"] = target.get("tested_as_submitted")
                outcome.target = _group_outcome(target)
                candidate_coverage = [
                    entry["check"]
                    for entry in expected.values()
                    if entry.get("provenance") == "candidate_collection_authorized"
                ]
                outcome.independently_verified = bool(
                    target.get("pristine")
                    and target.get("status") == "passed"
                    and not any(
                        (item or {}).get("provenance") == "candidate_collection_authorized"
                        for item in target_coverage
                    )
                )
                if candidate_coverage:
                    report["unknowns"].append(
                        "Required test coverage for "
                        + ", ".join(candidate_coverage)
                        + " came from the candidate's own collection (evaluator-authorized); "
                        "the candidate could have reduced it."
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
                        regression_coverage,
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
                config_errors = [
                    group["integrity_error"]
                    for group in (target, report.get("regression"))
                    if group and group.get("integrity_error")
                ]
                if config_errors:
                    # Contents other than the submitted patch would have been tested.
                    outcome.integrity = "violated"
                    outcome.independently_verified = False
                    integrity["passed"] = False
                    integrity["violations"].append(
                        {"path": "(configuration)", "rule": "evaluator_settings_not_isolated"}
                    )
                    report["explanations"].append(f"Integrity: {config_errors[0]}")
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
            coverage = check.get("coverage") or {}
            if coverage.get("unavailable_reason"):
                lines.append(
                    f"{label} check “{check['name']}” not evaluated: trusted expected test "
                    f"coverage could not be established ({coverage['unavailable_reason']})"
                )
                continue
            if coverage.get("not_collected_by_candidate"):
                lines.append(
                    f"{label} check “{check['name']}”: the patch removed required tests "
                    f"({', '.join(coverage['not_collected_by_candidate'][:10])}); required "
                    f"coverage comes from {coverage.get('provenance')}."
                )
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
    sections = [
        f"[{item['section']}] in {item['path']}"
        for item in report.get("integrity", {}).get("evaluator_sections", [])
        if item.get("action") == "restored_section"
    ]
    if sections:
        lines.append(
            "Test-runner settings changed by the patch were reset to the base commit; the "
            f"rest of each file was tested as submitted: {', '.join(sections)}."
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
    if outcome.get("timed_out"):
        return "Inconclusive: the checks did not finish within their limits."
    return (
        "Inconclusive: the checks passed, but the required test coverage was not "
        "established independently of the candidate."
    )


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
