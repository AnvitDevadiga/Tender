"""Trusted/untrusted repository boundary.

Everything a user points Tender at is untrusted: its working tree, its ignored
files, and especially its ``.git`` directory. A repository's local Git config
can name helpers that Git runs automatically (``core.fsmonitor``, filter
drivers, ``diff.external``, hooks, ``include.path``), so running even
``git status`` against the original metadata can execute repository-controlled
code on the host.

This module never runs Git against the source's own metadata. It copies the
``.git`` directory as plain bytes into a private temporary directory (rejecting
symlinks and special files), replaces the copied config with a minimal
Tender-written one, drops hooks, and runs every Git command with global/system
configuration disabled and helper settings overridden. Disposable workspaces are
then materialized from Git objects at an exact commit, so ignored files (for
example ``.env``) and uncommitted edits never reach an agent or a verifier.

The source directory is only read. Working-tree status is computed by pointing
the sanitized metadata copy at the source tree; any index refresh writes to the
private copy, not to the source.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .repository import RepositoryError

FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REVISION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._/\-]{0,199}$")

#: Overrides applied to every Git invocation, after any config file is read.
SAFE_GIT_FLAGS: tuple[str, ...] = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.sshCommand=false",
    "-c",
    "core.askPass=",
    "-c",
    "credential.helper=",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=cat",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.file.allow=always",
)


class RepositoryBoundaryError(RepositoryError):
    """A source repository is unsafe or unsupported for isolated evaluation."""


def safe_git_env(home: str | Path | None = None) -> dict[str, str]:
    """A minimal environment: no global/system config, prompts, or replace refs."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home) if home is not None else os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LC_ALL": "C",
    }


def run_safe_git(
    arguments: list[str],
    *,
    cwd: str | Path | None = None,
    git_dir: str | Path | None = None,
    work_tree: str | Path | None = None,
    input_text: str | None = None,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git with hardened configuration; raise RepositoryError on failure."""
    command = ["git", *SAFE_GIT_FLAGS]
    if git_dir is not None:
        command.append(f"--git-dir={git_dir}")
    if work_tree is not None:
        command.append(f"--work-tree={work_tree}")
    command.extend(arguments)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=safe_git_env(),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositoryError(f"git {arguments[0]} failed: {exc}") from exc
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip()[:1000]
        raise RepositoryError(f"git {arguments[0]} failed: {detail or completed.returncode}")
    return completed


@dataclass(frozen=True)
class SourceIdentity:
    """What Tender observed about the user's source repository (read-only)."""

    head_commit: str | None
    branch: str | None
    dirty: bool
    status_entries: tuple[str, ...]
    status_digest: str
    status_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "head_commit": self.head_commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "status_entries": list(self.status_entries),
            "status_entries_truncated": self.status_truncated,
            "status_digest": self.status_digest,
        }


@dataclass(frozen=True)
class SnapshotLimits:
    max_git_bytes: int = 1024 * 1024 * 1024
    max_git_files: int = 200_000
    max_tree_files: int = 100_000
    max_tree_bytes: int = 512 * 1024 * 1024


class TrustedRepository:
    """A sanitized, private copy of a source repository's Git metadata.

    Use as a context manager. ``materialize`` creates any number of pristine
    workspaces at one commit; each is independent of the others and of the
    source directory.
    """

    def __init__(self, source: str | Path, limits: SnapshotLimits | None = None) -> None:
        self.source = Path(source).expanduser().resolve(strict=True)
        self.limits = limits or SnapshotLimits()
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self._git_copy: Path | None = None

    def __enter__(self) -> TrustedRepository:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def git_copy(self) -> Path:
        if self._git_copy is None:
            raise RuntimeError("TrustedRepository is not open")
        return self._git_copy

    def open(self) -> None:
        if self._git_copy is not None:
            return
        if not self.source.is_dir():
            raise RepositoryBoundaryError("repository source must be a directory")
        metadata = self.source / ".git"
        info = metadata.lstat() if os.path.lexists(metadata) else None
        if info is None:
            raise RepositoryBoundaryError("repository source must be a Git repository root")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RepositoryBoundaryError(
                "repository source must be a standalone Git repository "
                "(linked worktrees, submodule checkouts, and symlinked .git are unsupported)"
            )
        self._temp = tempfile.TemporaryDirectory(prefix="tender-git-")
        try:
            target = Path(self._temp.name) / "metadata.git"
            self._copy_metadata(metadata, target)
            self._sanitize_metadata(target)
            self._git_copy = target
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._temp is not None:
            self._temp.cleanup()
        self._temp = None
        self._git_copy = None

    # -- inspection -------------------------------------------------------

    def resolve_commit(self, revision: str) -> str:
        """Resolve a revision to a full commit ID that exists in this repository."""
        if not _REVISION.fullmatch(revision) or ".." in revision:
            raise RepositoryBoundaryError("revision must be a commit ID or simple ref name")
        completed = run_safe_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
            git_dir=self.git_copy,
            check=False,
        )
        commit = completed.stdout.strip().lower()
        if completed.returncode or not FULL_COMMIT.fullmatch(commit):
            raise RepositoryBoundaryError(f"commit not found in repository: {revision}")
        return commit

    def head_commit(self) -> str | None:
        completed = run_safe_git(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            git_dir=self.git_copy,
            check=False,
        )
        commit = completed.stdout.strip().lower()
        return commit if completed.returncode == 0 and FULL_COMMIT.fullmatch(commit) else None

    def identity(self, *, max_entries: int = 200) -> SourceIdentity:
        """Observe HEAD and working-tree status without executing repository helpers."""
        head = self.head_commit()
        branch_result = run_safe_git(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], git_dir=self.git_copy, check=False
        )
        branch = (branch_result.stdout.strip() or None) if branch_result.returncode == 0 else None
        status = run_safe_git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"],
            git_dir=self.git_copy,
            work_tree=self.source,
            cwd=self.source,
            timeout=120,
        ).stdout
        entries = [entry for entry in status.split("\0") if entry]
        return SourceIdentity(
            head_commit=head,
            branch=branch,
            dirty=bool(entries),
            status_entries=tuple(entry[:300] for entry in entries[:max_entries]),
            status_digest=hashlib.sha256(status.encode("utf-8", "surrogateescape")).hexdigest(),
            status_truncated=len(entries) > max_entries,
        )

    # -- materialization --------------------------------------------------

    def materialize(self, commit: str, destination: str | Path) -> Path:
        """Create a pristine workspace whose HEAD is exactly ``commit``.

        Only tracked content at that commit is written. Symlinks and submodules
        in the tree are rejected; the workspace's Git metadata has no remote,
        no hooks, and no helper configuration.
        """
        commit = self.resolve_commit(commit)
        target = Path(destination)
        if target.exists():
            raise RepositoryBoundaryError(f"workspace destination already exists: {target}")
        self._check_tree(commit)
        target.parent.mkdir(parents=True, exist_ok=True)
        template = Path(tempfile.mkdtemp(prefix="tender-git-template-"))
        try:
            run_safe_git(
                [
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--no-hardlinks",
                    f"--template={template}",
                    "--",
                    str(self.git_copy),
                    str(target),
                ],
                timeout=300,
            )
        finally:
            shutil.rmtree(template, ignore_errors=True)
        try:
            run_safe_git(["remote", "remove", "origin"], cwd=target)
            run_safe_git(["checkout", "--quiet", "--detach", "--force", commit], cwd=target)
            shutil.rmtree(target / ".git" / "hooks", ignore_errors=True)
            actual = run_safe_git(["rev-parse", "HEAD"], cwd=target).stdout.strip().lower()
            if actual != commit:
                raise RepositoryBoundaryError("materialized workspace does not match the commit")
            dirty = run_safe_git(
                ["status", "--porcelain=v1", "--untracked-files=all"], cwd=target
            ).stdout
            if dirty.strip():
                raise RepositoryBoundaryError("materialized workspace is not clean")
            self._reject_special_files(target)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    # -- internals --------------------------------------------------------

    def _check_tree(self, commit: str) -> None:
        listing = run_safe_git(
            ["ls-tree", "-r", "-z", "-l", "--full-tree", commit],
            git_dir=self.git_copy,
            timeout=120,
        ).stdout
        files = total = 0
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            parts = meta.split()
            if len(parts) < 4:
                continue
            mode, kind, size = parts[0], parts[1], parts[3]
            if mode == "120000":
                raise RepositoryBoundaryError(f"repository tree contains a symbolic link: {path}")
            if mode == "160000" or kind == "commit":
                raise RepositoryBoundaryError(f"repository tree contains a submodule: {path}")
            files += 1
            total += int(size) if size.isdigit() else 0
            if files > self.limits.max_tree_files:
                raise RepositoryBoundaryError(
                    f"repository tree exceeds file limit ({self.limits.max_tree_files})"
                )
            if total > self.limits.max_tree_bytes:
                raise RepositoryBoundaryError(
                    f"repository tree exceeds size limit ({self.limits.max_tree_bytes} bytes)"
                )

    def _copy_metadata(self, source: Path, target: Path) -> None:
        count = total = 0
        for current, dirs, names in os.walk(source, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            if relative.parts[:1] == ("hooks",):
                dirs[:] = []
                continue
            (target / relative).mkdir(parents=True, exist_ok=True)
            for name in list(dirs):
                if (current_path / name).is_symlink():
                    raise RepositoryBoundaryError(
                        f"Git metadata contains a symbolic link: {relative / name}"
                    )
            for name in names:
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise RepositoryBoundaryError(
                        f"Git metadata contains a symbolic link: {relative / name}"
                    )
                if not stat.S_ISREG(info.st_mode):
                    raise RepositoryBoundaryError(
                        f"Git metadata contains a special file: {relative / name}"
                    )
                count += 1
                total += info.st_size
                if count > self.limits.max_git_files:
                    raise RepositoryBoundaryError(
                        f"Git metadata exceeds file limit ({self.limits.max_git_files})"
                    )
                if total > self.limits.max_git_bytes:
                    raise RepositoryBoundaryError(
                        f"Git metadata exceeds size limit ({self.limits.max_git_bytes} bytes)"
                    )
                shutil.copyfile(path, target / relative / name, follow_symlinks=False)

    @staticmethod
    def _sanitize_metadata(target: Path) -> None:
        for unsupported in ("commondir", "gitdir"):
            if (target / unsupported).exists():
                raise RepositoryBoundaryError(
                    "linked worktrees are unsupported; use the main repository"
                )
        if (target / "objects" / "info" / "alternates").exists():
            raise RepositoryBoundaryError(
                "repositories with object alternates are unsupported (objects live elsewhere)"
            )
        original = (target / "config").read_text(encoding="utf-8", errors="replace").lower()
        object_format = re.search(r"objectformat\s*=\s*(\w+)", original)
        if object_format and object_format.group(1) != "sha1":
            raise RepositoryBoundaryError("only SHA-1 Git repositories are supported")
        # Replace the untrusted config entirely: no includes, filters, helpers,
        # remotes, or worktree overrides survive.
        (target / "config").write_text(
            "[core]\n\trepositoryformatversion = 0\n\tbare = true\n", encoding="utf-8"
        )
        for removable in ("hooks", "info/attributes", "FETCH_HEAD", "sequencer", "rebase-merge"):
            path = target / removable
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()

    @staticmethod
    def _reject_special_files(root: Path) -> None:
        for current, dirs, names in os.walk(root, followlinks=False):
            current_path = Path(current)
            if current_path == root:
                dirs[:] = [name for name in dirs if name != ".git"]
            for name in [*dirs, *names]:
                info = (current_path / name).lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise RepositoryBoundaryError(
                        "workspace contains a symbolic link: "
                        f"{(current_path / name).relative_to(root)}"
                    )
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise RepositoryBoundaryError(
                        "workspace contains a special file: "
                        f"{(current_path / name).relative_to(root)}"
                    )
