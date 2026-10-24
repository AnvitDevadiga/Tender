from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .snapshot import run_safe_git


@dataclass(frozen=True)
class PatchArtifact:
    diff: str
    changed_files: tuple[str, ...]
    sha256: str
    protected_file_changes: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.protected_file_changes


class PatchCapture:
    """Capture the agent's exact tracked-file diff and enforce protected paths."""

    def __init__(self, protected_paths: set[str] | None = None) -> None:
        self.protected_paths = protected_paths or {
            ".tender/evaluator",
            ".tender/benchmark.toml",
        }

    def capture(self, root: str | Path) -> PatchArtifact:
        root = Path(root).resolve()
        # Hardened Git: helper settings in the workspace config cannot execute.
        run_safe_git(["add", "-N", "--", "."], cwd=root)
        diff = run_safe_git(
            ["diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"], cwd=root
        ).stdout
        status = run_safe_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], cwd=root
        ).stdout
        changed = tuple(sorted(entry[3:] for entry in status.split("\0") if len(entry) >= 4))
        protected = tuple(sorted(path for path in changed if self._is_protected(path)))
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        return PatchArtifact(
            diff=diff, changed_files=changed, sha256=digest, protected_file_changes=protected
        )

    def _is_protected(self, path: str) -> bool:
        normalized = path.casefold()
        parts = Path(normalized).parts
        filename = Path(normalized).name
        sensitive = (
            any(part in {".aws", ".ssh"} or part.startswith(".env") for part in parts)
            or "credential" in filename
            or "secret" in filename
            or filename.startswith("token.")
            or filename.endswith((".token", "_token", "-token", ".pem", ".key", ".p12", ".pfx"))
        )
        return sensitive or any(
            normalized == protected.casefold()
            or normalized.startswith(protected.casefold().rstrip("/") + "/")
            for protected in self.protected_paths
        )
