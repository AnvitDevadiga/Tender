from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tender.github import GitHubIssueClient
from tender.repository import RepositoryError, RepositoryManager
from tender.sandbox import DockerSandbox


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo/issues/1",
        "https://github.com.evil.example/owner/repo/issues/1",
        "https://user@github.com/owner/repo/issues/1",
        "https://github.com/owner/repo/issues/1?redirect=https://127.0.0.1",
        "https://github.com/owner/repo/issues/1#fragment",
        "https://github.com/owner/repo/pull/1",
        "https://github.com/owner/repo/issues/not-a-number",
    ],
)
def test_github_issue_url_rejects_noncanonical_or_unsupported_urls(url: str) -> None:
    with pytest.raises(ValueError):
        GitHubIssueClient.parse_url(url)


def test_github_issue_url_parses_canonical_url() -> None:
    assert GitHubIssueClient.parse_url("https://github.com/owner/repo/issues/123") == (
        "owner",
        "repo",
        123,
    )


def test_isolated_workspace_copy_rejects_symlinks(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RepositoryError, match="symbolic link"):
        RepositoryManager.isolated_copy(source, tmp_path / "copy")


def test_repository_validation_requires_a_clean_git_root(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    (source / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)

    commit = RepositoryManager.validate_clean_git_repository(source)
    assert len(commit) == 40

    (source / "app.py").write_text("return 2\n", encoding="utf-8")
    with pytest.raises(RepositoryError, match="must be clean"):
        RepositoryManager.validate_clean_git_repository(source)


def test_isolated_workspace_copy_removes_local_git_helpers(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    config = source / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[diff]\n\texternal = touch /tmp/tender-unsafe\n",
        encoding="utf-8",
    )

    copy = RepositoryManager.isolated_copy(source, tmp_path / "copy")

    copied_config = (copy / ".git" / "config").read_text(encoding="utf-8")
    assert "external" not in copied_config


def test_isolated_workspace_copy_enforces_size_limit(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * 16)
    with pytest.raises(RepositoryError, match="size limit"):
        RepositoryManager.isolated_copy(source, tmp_path / "copy", max_bytes=8)


def test_docker_timeout_removes_started_container(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    exec_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "create":
            Path(command[command.index("--cidfile") + 1]).write_text("deadbeef\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_bounded(command, **kwargs):
        exec_commands.append(command)
        if command[-1] == "sleep 10":
            return None, "partial", "", True
        return 0, "", "", False

    monkeypatch.setattr("tender.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("tender.sandbox._run_bounded", fake_bounded)
    result = DockerSandbox(tmp_path, timeout_seconds=1).run("sleep 10")

    assert result.timed_out
    assert [call[1] for call in calls] == ["create", "start", "exec", "rm"]
    assert calls[-1][-1] == "deadbeef"
    assert exec_commands[-1][-1] == "sleep 10"


def test_docker_commands_share_one_disposable_container(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1] == "create":
            return SimpleNamespace(returncode=0, stdout="cafebabe\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    def fake_bounded(command, **kwargs):
        calls.append(command)
        return 0, "ok", "", False

    monkeypatch.setattr("tender.sandbox.subprocess.run", fake_run)
    monkeypatch.setattr("tender.sandbox._run_bounded", fake_bounded)
    sandbox = DockerSandbox(tmp_path)
    sandbox.run("python3 -m pip install -e .")
    sandbox.run("python3 -m pytest -q")
    sandbox.destroy()

    exec_calls = [call for call in calls if call[1] == "exec"]
    assert len(exec_calls) == 3  # initialize, setup, test
    assert exec_calls[0][2] == "cafebabe"
    assert exec_calls[1][4] == exec_calls[2][4] == "cafebabe"
    assert calls[-1][1:] == ["rm", "-f", "cafebabe"]


def test_docker_sandbox_image_can_be_configured_for_repository_tools(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TENDER_SANDBOX_IMAGE", "tender-sandbox:test")

    sandbox = DockerSandbox(tmp_path)

    assert sandbox.image == "tender-sandbox:test"


@pytest.mark.asyncio
async def test_github_issue_fetch_rejects_redirect_to_other_host(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://example.com/redirect"

        def read(self, size):
            return b"{}"

    monkeypatch.setattr("tender.github.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="redirected"):
        await GitHubIssueClient().fetch("https://github.com/owner/repo/issues/1")


@pytest.mark.asyncio
async def test_github_issue_fetch_caps_response_size(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def geturl(self):
            return "https://api.github.com/repos/owner/repo/issues/1"

        def read(self, size):
            return b"x" * size

    monkeypatch.setattr("tender.github.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(ValueError, match="1 MB limit"):
        await GitHubIssueClient().fetch("https://github.com/owner/repo/issues/1")
