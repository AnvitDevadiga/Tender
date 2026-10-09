from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks" / "tasks.jsonl"
BASELINE_BRANCH = "forgebench-baseline"
AUTHOR_NAME = "ForgeBench Fixture Builder"
AUTHOR_EMAIL = "fixtures@example.invalid"
FIXED_DATE = "@1760000000 +0000"


def run_git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def prepare(workspace: Path, expected_commit: str) -> str:
    metadata = workspace / ".git"
    fresh_repository = not metadata.exists()
    if fresh_repository:
        run_git(workspace, "init", "--quiet", "--initial-branch", BASELINE_BRANCH)

    branches = run_git(workspace, "branch", "--list", BASELINE_BRANCH)
    if branches:
        run_git(workspace, "checkout", "--quiet", BASELINE_BRANCH)
        if run_git(workspace, "status", "--porcelain"):
            raise RuntimeError(f"fixture has uncommitted changes: {workspace}")
        actual = run_git(workspace, "rev-parse", "HEAD")
        if actual != expected_commit:
            raise RuntimeError(
                f"fixture pin mismatch for {workspace}: expected {expected_commit}, found {actual}"
            )
        return actual

    if not fresh_repository:
        dirty = run_git(workspace, "status", "--porcelain")
        allowed = {".gitignore"}
        if any(line[3:] not in allowed for line in dirty.splitlines()):
            raise RuntimeError(f"refusing to replace a dirty fixture baseline: {workspace}")
        run_git(workspace, "checkout", "--quiet", "--orphan", BASELINE_BRANCH)
    run_git(workspace, "add", "--all")
    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_AUTHOR_DATE": FIXED_DATE,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_DATE": FIXED_DATE,
        }
    )
    run_git(
        workspace,
        "-c",
        f"user.name={AUTHOR_NAME}",
        "-c",
        f"user.email={AUTHOR_EMAIL}",
        "commit",
        "--quiet",
        "-m",
        "ForgeBench deterministic fixture baseline",
        env=commit_env,
    )
    actual = run_git(workspace, "rev-parse", "HEAD")
    if actual != expected_commit:
        raise RuntimeError(
            f"fixture pin mismatch for {workspace}: expected {expected_commit}, generated {actual}; "
            "review the fixture change and update every task pin deliberately"
        )
    return actual


def main() -> None:
    tasks = [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    workspaces: dict[Path, set[str]] = {}
    for task in tasks:
        raw_workspace = Path(task["workspace"])
        workspace = (ROOT / raw_workspace).resolve()
        workspace.relative_to(ROOT)
        commit = task.get("base_commit") or task["issue"].get("base_commit")
        if not isinstance(commit, str) or len(commit) != 40:
            raise ValueError(f"task {task.get('task_id')} has no full base commit pin")
        workspaces.setdefault(workspace, set()).add(commit)

    for workspace, commits in sorted(workspaces.items()):
        if len(commits) != 1:
            raise ValueError(f"tasks for {workspace} disagree about their base commit")
        if not workspace.is_dir():
            raise FileNotFoundError(f"fixture workspace not found: {workspace}")
        commit = prepare(workspace, next(iter(commits)))
        print(f"{workspace.relative_to(ROOT)}: {commit}")


if __name__ == "__main__":
    main()
