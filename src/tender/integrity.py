"""Evaluator integrity: an agent must not change the rules that judge its work.

Two ownership classes exist inside a candidate repository:

* **Agent-editable** project files: anything not covered below.
* **Evaluator-owned** files, split into two policies:

  - ``protected_paths``: a candidate patch that touches one of these is
    rejected outright (``integrity_violation``) and never verified.
  - ``restored_paths``: test-runner configuration and evaluator test files.
    After the candidate patch is applied to a pristine verifier workspace,
    these paths are reset to their base-commit content (files the patch added
    are deleted). The candidate can therefore change them for its own
    iteration, but verification always runs against the trusted versions.
    Restoring can only cause a false *rejection*, never a false acceptance.

Hidden evaluator assets (for example held-out tests) live outside the agent
workspace and are written into the verifier only after the patch is applied.

Patches that add Python modules which would shadow the test tooling
(``pytest.py``, ``sitecustomize.py``, ``*.pth`` …) are integrity violations
regardless of path, because filename policy alone cannot express "do not
replace the test runner".
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .snapshot import run_safe_git

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (".tender/**",)
DEFAULT_RESTORED_PATHS: tuple[str, ...] = (
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "noxfile.py",
    "tests/**",
    "test/**",
)
#: Mixed-purpose configuration files. Only the named test-runner section is
#: evaluator-owned; the rest (application, build, tool settings) is candidate
#: content and is tested exactly as submitted.
DEFAULT_EVALUATOR_SECTIONS: dict[str, str] = {
    "pyproject.toml": "tool.pytest",
    "setup.cfg": "tool:pytest",
    "tox.ini": "pytest",
}
#: Module names that would replace the test runner or interpreter start-up hooks.
SHADOWING_NAMES = frozenset(
    {
        "pytest.py",
        "pytest",
        "_pytest",
        "py.py",
        "pluggy",
        "pluggy.py",
        "unittest",
        "unittest.py",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
SHADOWING_SUFFIXES = (".pth",)


def _normalize(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def matches(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Case-insensitive glob match where ``**`` spans directories."""
    normalized = _normalize(path).casefold()
    for pattern in patterns:
        candidate = pattern.casefold().strip()
        if not candidate:
            continue
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
        if candidate.endswith("/**") and (
            normalized == candidate[:-3] or normalized.startswith(candidate[:-2])
        ):
            return True
        if candidate.startswith("**/") and fnmatch.fnmatchcase(
            PurePosixPath(normalized).name, candidate[3:]
        ):
            return True
    return False


class IntegrityPolicy(BaseModel):
    """Evaluator ownership rules recorded with every run."""

    model_config = ConfigDict(extra="forbid")

    protected_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PROTECTED_PATHS), max_length=200
    )
    restored_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESTORED_PATHS), max_length=200
    )
    evaluator_sections: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_EVALUATOR_SECTIONS), max_length=20
    )
    reject_tool_shadowing: bool = True

    @field_validator("evaluator_sections")
    @classmethod
    def _sections(cls, value: dict[str, str]) -> dict[str, str]:
        for path, section in value.items():
            if path.startswith("/") or ".." in PurePosixPath(path).parts or not path.strip():
                raise ValueError("evaluator section files must be repository-relative")
            if not path.endswith((".toml", ".cfg", ".ini")):
                raise ValueError("evaluator sections are supported in .toml, .cfg, and .ini files")
            if not section.strip() or len(section) > 200:
                raise ValueError("evaluator section names must be non-empty")
        return value

    @field_validator("protected_paths", "restored_paths")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 300:
                raise ValueError("integrity patterns must be non-empty strings")
            if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
                raise ValueError("integrity patterns must be repository-relative")
        return value


@dataclass(frozen=True)
class IntegrityFinding:
    path: str
    rule: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "rule": self.rule}


@dataclass(frozen=True)
class IntegrityReport:
    """What the integrity gate observed for one candidate patch."""

    violations: tuple[IntegrityFinding, ...] = ()
    restored: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    assets: tuple[dict[str, object], ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "violations": [item.as_dict() for item in self.violations],
            "restored_from_base": list(self.restored),
            "removed_patch_additions": list(self.removed),
            "evaluator_assets": list(self.assets),
        }


@dataclass(frozen=True)
class EvaluatorAssets:
    """Trusted files written into the verifier after the patch is applied.

    Content is read once, before any candidate executes, and its digest is
    recorded so the report shows exactly which hidden checks were used.
    """

    files: tuple[tuple[str, bytes], ...] = field(default_factory=tuple)

    @classmethod
    def from_directory(cls, directory: str | Path, *, max_bytes: int = 8 * 1024 * 1024):
        root = Path(directory).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("evaluator assets must be a directory")
        files: list[tuple[str, bytes]] = []
        total = 0
        for current, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not (Path(current) / name).is_symlink())
            for name in sorted(names):
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if "__pycache__" in path.parts or name.endswith((".pyc", ".pyo")):
                    continue
                data = path.read_bytes()
                total += len(data)
                if total > max_bytes:
                    raise ValueError("evaluator assets exceed the size limit")
                files.append((path.relative_to(root).as_posix(), data))
        return cls(tuple(files))

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(path for path, _ in self.files)

    def manifest(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in self.files
        )

    def install(self, root: Path) -> None:
        for relative, data in self.files:
            target = (root / relative).resolve()
            target.relative_to(root.resolve())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def check_patch_paths(
    changed_files: tuple[str, ...] | list[str],
    policy: IntegrityPolicy,
    assets: EvaluatorAssets | None = None,
) -> tuple[IntegrityFinding, ...]:
    """Find candidate changes that are never allowed, before anything executes."""
    findings: list[IntegrityFinding] = []
    asset_paths = tuple(assets.paths) if assets else ()
    for raw in changed_files:
        path = _normalize(raw)
        parts = [part.casefold() for part in PurePosixPath(path).parts]
        if any(part == ".git" for part in parts):
            findings.append(IntegrityFinding(path, "git_metadata"))
            continue
        if matches(path, policy.protected_paths):
            findings.append(IntegrityFinding(path, "protected_path"))
            continue
        if asset_paths and matches(path, asset_paths):
            findings.append(IntegrityFinding(path, "evaluator_asset"))
            continue
        if policy.reject_tool_shadowing and (
            any(part in SHADOWING_NAMES for part in parts)
            or PurePosixPath(path).name.casefold().endswith(SHADOWING_SUFFIXES)
        ):
            findings.append(IntegrityFinding(path, "test_tool_shadowing"))
    return tuple(findings)


class EvaluatorConfigError(ValueError):
    """Evaluator settings could not be isolated from candidate configuration."""


def restore_evaluator_paths(
    root: Path,
    base_commit: str,
    changed_files: tuple[str, ...] | list[str],
    policy: IntegrityPolicy,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[dict[str, str], ...]]:
    """Reset evaluator-owned files and sections the patch touched.

    Returns (restored files, removed patch additions, section actions). Raises
    EvaluatorConfigError when evaluator settings cannot be isolated exactly, so
    callers fail closed instead of testing silently altered contents.
    """
    restored: list[str] = []
    removed: list[str] = []
    for raw in sorted(set(changed_files)):
        path = _normalize(raw)
        if not matches(path, policy.restored_paths):
            continue
        exists = run_safe_git(
            ["cat-file", "-e", f"{base_commit}:{path}"], cwd=root, check=False
        ).returncode
        target = (root / path).resolve()
        target.relative_to(root.resolve())
        if exists == 0:
            run_safe_git(["checkout", base_commit, "--", path], cwd=root)
            restored.append(path)
        elif target.is_file():
            target.unlink()
            removed.append(path)
    sections = restore_evaluator_sections(root, base_commit, changed_files, policy, restored)
    return tuple(restored), tuple(removed), tuple(sections)


def restore_evaluator_sections(
    root: Path,
    base_commit: str,
    changed_files: tuple[str, ...] | list[str],
    policy: IntegrityPolicy,
    already_restored: list[str] | tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """Put the base commit's test-runner section into candidate config files.

    Everything outside the section stays exactly as the candidate wrote it. The
    result is re-parsed and compared: the section must equal the base version
    and every other value must equal the candidate version.
    """
    changed = {_normalize(path) for path in changed_files}
    actions: list[dict[str, str]] = []
    for path, section in sorted(policy.evaluator_sections.items()):
        if path not in changed or path in already_restored:
            continue
        shown = run_safe_git(["show", f"{base_commit}:{path}"], cwd=root, check=False)
        base_text = shown.stdout if shown.returncode == 0 else None
        target = (root / path).resolve()
        target.relative_to(root.resolve())
        candidate_text = target.read_text(encoding="utf-8") if target.is_file() else None
        kind = "toml" if path.endswith(".toml") else "ini"
        base_section = _section_value(kind, base_text, section, path) if base_text else None
        if candidate_text is None:
            if base_section:
                raise EvaluatorConfigError(
                    f"the patch deletes {path}, which holds evaluator settings [{section}]"
                )
            continue
        if _section_value(kind, candidate_text, section, path) == base_section:
            actions.append({"path": path, "section": section, "action": "unchanged"})
            continue
        base_block = _section_blocks(kind, base_text, section)[1] if base_text else ""
        remainder = _section_blocks(kind, candidate_text, section)[0]
        spliced = remainder.rstrip("\n") + "\n"
        if base_block:
            spliced += "\n" + base_block.rstrip("\n") + "\n"
        if _section_value(kind, spliced, section, path) != base_section or _without_section(
            kind, spliced, section, path
        ) != _without_section(kind, candidate_text, section, path):
            raise EvaluatorConfigError(
                f"cannot isolate evaluator settings [{section}] in {path}; use the standard "
                "table/section form"
            )
        target.write_text(spliced, encoding="utf-8")
        actions.append({"path": path, "section": section, "action": "restored_section"})
    return actions


def _header_key(kind: str, line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip() if kind == "toml" else line.strip()
    if not stripped.startswith("["):
        return None
    if kind == "toml":
        inner = stripped.strip("[]")
        return ".".join(part.strip().strip('"').strip("'") for part in inner.split("."))
    return stripped.strip("[]").strip()


def _in_section(kind: str, key: str, section: str) -> bool:
    if kind == "toml":
        return key == section or key.startswith(section + ".")
    return key == section


def _section_blocks(kind: str, text: str, section: str) -> tuple[str, str]:
    """Split text into (everything else, the section's header blocks)."""
    keep: list[str] = []
    block: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        key = _header_key(kind, line)
        if key is not None:
            inside = _in_section(kind, key, section)
        (block if inside else keep).append(line)
    return "".join(keep), "".join(block)


def _parse(kind: str, text: str, path: str) -> dict:
    import configparser
    import tomllib

    try:
        if kind == "toml":
            return tomllib.loads(text)
        parser = configparser.RawConfigParser(strict=True, interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        parser.read_string(text)
        return {name: dict(parser.items(name)) for name in parser.sections()}
    except (tomllib.TOMLDecodeError, configparser.Error) as exc:
        raise EvaluatorConfigError(f"cannot parse {path} to isolate evaluator settings") from exc


def _section_value(kind: str, text: str | None, section: str, path: str) -> object:
    if text is None:
        return None
    data = _parse(kind, text, path)
    if kind == "ini":
        return data.get(section)
    node: object = data
    for part in section.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _without_section(kind: str, text: str, section: str, path: str) -> dict:
    data = _parse(kind, text, path)
    if kind == "ini":
        data.pop(section, None)
        return data
    parts = section.split(".")
    trail = [data]
    node = data
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, dict) else None
        if not isinstance(child, dict):
            return data
        trail.append(child)
        node = child
    node.pop(parts[-1], None)
    for parent, part in zip(reversed(trail[:-1]), reversed(parts[:-1])):
        if parent.get(part) == {}:
            del parent[part]
    return data
