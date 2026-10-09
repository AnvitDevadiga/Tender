from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
            ".forgebench/evaluator",
            ".forgebench/benchmark.toml",
        }

    def capture(self, root: str | Path) -> PatchArtifact:
        root = Path(root).resolve()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
        }
        subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "add", "-N", "--", "."],
            cwd=root,
            check=True,
            capture_output=True,
            env=env,
        )
        diff = subprocess.check_output(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--binary",
            ],
            cwd=root,
            text=True,
            env=env,
        )
        status = subprocess.check_output(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1"],
            cwd=root,
            text=True,
            env=env,
        )
        changed = tuple(sorted(line[3:] for line in status.splitlines() if len(line) >= 4))
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
