from __future__ import annotations

import os
import re
import shutil
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


class RepositoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepositorySnapshot:
    source: str
    path: Path
    commit: str
    license: str | None = None


@dataclass(frozen=True)
class RepositoryMap:
    file_count: int
    languages: dict[str, int]
    entry_points: tuple[str, ...]
    test_files: tuple[str, ...]
    config_files: tuple[str, ...]
    dependency_manifests: tuple[str, ...]
    command_hints: tuple[str, ...]


class RepositoryAnalyzer:
    """Build a bounded, non-executing summary of repository structure."""

    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "vendor"}
    extensions = {
        ".py": "Python",
        ".pyi": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".go": "Go",
        ".rs": "Rust",
        ".java": "Java",
        ".kt": "Kotlin",
        ".rb": "Ruby",
        ".php": "PHP",
        ".cs": "C#",
        ".cpp": "C++",
        ".cc": "C++",
        ".c": "C",
        ".h": "C/C++",
        ".swift": "Swift",
        ".scala": "Scala",
        ".sh": "Shell",
    }
    entry_names = {
        "main.py",
        "app.py",
        "__main__.py",
        "main.ts",
        "index.ts",
        "index.js",
        "main.go",
        "main.rs",
        "manage.py",
    }
    config_names = {
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "package.json",
        "tsconfig.json",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "Dockerfile",
        "docker-compose.yml",
        ".github/workflows",
    }
    dependency_names = {
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "uv.lock",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
    }

    def analyze(self, root: str | Path, *, max_files: int = 20_000) -> RepositoryMap:
        root = Path(root).resolve()
        languages: Counter[str] = Counter()
        entries: list[str] = []
        tests: list[str] = []
        configs: list[str] = []
        dependencies: list[str] = []
        file_count = 0
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            dirs[:] = sorted(
                name
                for name in dirs
                if name not in self.ignored and not (current_path / name).is_symlink()
            )
            for name in sorted(names):
                path = current_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root)
                file_count += 1
                if file_count > max_files:
                    raise RepositoryError(f"repository exceeds analysis file limit ({max_files})")
                if path.suffix.lower() in self.extensions:
                    languages[self.extensions[path.suffix.lower()]] += 1
                rel = relative.as_posix()
                if path.name in self.entry_names:
                    entries.append(rel)
                if (
                    path.name.startswith("test_")
                    or "/tests/" in f"/{rel}/"
                    or path.name.endswith("_test.go")
                ):
                    tests.append(rel)
                if path.name in self.config_names or path.name.startswith(".config."):
                    configs.append(rel)
                if path.name in self.dependency_names:
                    dependencies.append(rel)
        command_hints: list[str] = []
        if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
            command_hints.append("pytest -q")
        if (root / "package.json").is_file():
            command_hints.append("npm test (inspect package.json scripts first)")
        if (root / "Cargo.toml").is_file():
            command_hints.append("cargo test")
        if (root / "go.mod").is_file():
            command_hints.append("go test ./...")
        return RepositoryMap(
            file_count=file_count,
            languages=dict(sorted(languages.items())),
            entry_points=tuple(entries[:100]),
            test_files=tuple(tests[:200]),
            config_files=tuple(configs[:100]),
            dependency_manifests=tuple(dependencies[:100]),
            command_hints=tuple(command_hints),
        )


class RepositoryManager:
    """Clone or inspect a repository and return an immutable commit snapshot."""

    _commit_pattern = re.compile(r"^[0-9a-f]{7,64}$")

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_clean_git_repository(source: str | Path) -> str:
        """Require a standalone, clean Git root and return its immutable base commit.

        Git runs only against a sanitized copy of the metadata, so repository
        helpers (fsmonitor, filters, hooks) never execute on the host.
        """
        from .snapshot import TrustedRepository

        source_path = Path(source).resolve(strict=True)
        if not (source_path / ".git").is_dir() or (source_path / ".git").is_symlink():
            raise RepositoryError("workspace must be a standalone Git repository root")
        with TrustedRepository(source_path) as repository:
            identity = repository.identity()
        if identity.head_commit is None:
            raise RepositoryError("workspace HEAD is not a full Git commit")
        if identity.dirty:
            raise RepositoryError("workspace must be clean before a coding-agent run")
        return identity.head_commit

    @staticmethod
    def isolated_copy(
        source: str | Path,
        destination: str | Path,
        *,
        max_bytes: int = 512 * 1024 * 1024,
        max_files: int = 100_000,
    ) -> Path:
        """Copy a repository into a disposable run directory after rejecting special files."""
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_dir():
            raise RepositoryError("workspace must be a directory")
        count = total_bytes = 0
        for path in source_path.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RepositoryError(
                    f"workspace contains a symbolic link: {path.relative_to(source_path)}"
                )
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RepositoryError(
                    f"workspace contains a non-regular file: {path.relative_to(source_path)}"
                )
            count += 1
            total_bytes += info.st_size
            if count > max_files:
                raise RepositoryError(f"workspace exceeds file limit ({max_files})")
            if total_bytes > max_bytes:
                raise RepositoryError(f"workspace exceeds size limit ({max_bytes} bytes)")
        target = Path(destination)
        shutil.copytree(source_path, target, symlinks=True)
        for path in [target, *target.rglob("*")]:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RepositoryError(
                    "workspace changed during copy and contains a symbolic link: "
                    f"{path.relative_to(target)}"
                )
            if stat.S_ISDIR(info.st_mode):
                path.chmod(0o755)
            elif stat.S_ISREG(info.st_mode):
                path.chmod(0o755 if info.st_mode & 0o111 else 0o644)
            else:
                raise RepositoryError(
                    "workspace changed during copy and contains a special file: "
                    f"{path.relative_to(target)}"
                )
        git_dir = target / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir / "hooks", ignore_errors=True)
            if not git_dir.is_dir():
                raise RepositoryError(
                    "workspace must contain a standalone Git repository, not a linked worktree"
                )
            config = git_dir / "config"
            if not config.is_file():
                raise RepositoryError("Git repository is missing its local config")
            config.write_text(
                "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n"
                "\tbare = false\n\tlogallrefupdates = true\n",
                encoding="utf-8",
            )
        return target

    def snapshot(
        self, source: str, destination: str, commit: str | None = None
    ) -> RepositorySnapshot:
        """Materialize a local repository at a pinned commit.

        Remote sources are disabled until isolation for arbitrary remote code is
        reviewed; clone them yourself into a trusted local directory first.
        """
        from .snapshot import TrustedRepository

        if "://" in source or re.match(r"^[\w.-]+@[\w.-]+:", source) or source.startswith("ext:"):
            raise RepositoryError("remote repository sources are disabled; use a local path")
        if commit and not self._commit_pattern.fullmatch(commit):
            raise RepositoryError("commit must be a hexadecimal Git object id")
        target = self.workspace_root / destination
        if target.exists():
            raise RepositoryError(f"destination already exists: {target}")
        with TrustedRepository(source) as repository:
            resolved = repository.resolve_commit(commit) if commit else repository.head_commit()
            if resolved is None:
                raise RepositoryError("repository has no commit to snapshot")
            repository.materialize(resolved, target)
        return RepositorySnapshot(
            source=source,
            path=target,
            commit=resolved,
            license=self._find_license(target),
        )

    @staticmethod
    def _find_license(path: Path) -> str | None:
        for candidate in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
            if (path / candidate).is_file():
                return candidate
        return None
