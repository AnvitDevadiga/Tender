import subprocess

from forgebench.grader import TestGrader
from forgebench.repository import RepositoryManager
from forgebench.retrieval import TextRetriever
from forgebench.sandbox import MAX_COMMAND_OUTPUT_BYTES, CommandResult, Sandbox


def make_repo(path) -> str:
    path.mkdir()
    (path / "app.py").write_text("def greeting():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


def test_repository_snapshot_is_pinned_and_searchable(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    snapshot = RepositoryManager(tmp_path / "snapshots").snapshot(str(source), "issue-1", commit)
    hits = TextRetriever().search(snapshot.path, "greeting")
    assert snapshot.commit == commit
    assert hits[0].path == "app.py"


def test_sandbox_and_grader_execute_commands(tmp_path) -> None:
    make_repo(tmp_path / "repo")
    grade = TestGrader().grade(Sandbox(tmp_path / "repo"), ["python3 -c 'print(\"ok\")'"])
    assert grade.passed is True
    assert grade.reason == "all_commands_passed"


def test_sandbox_caps_command_output(tmp_path) -> None:
    result = Sandbox(tmp_path).run("python3 -c 'print(\"x\" * 1000000)'")

    assert result.passed
    assert len(result.stdout.encode("utf-8")) <= MAX_COMMAND_OUTPUT_BYTES
    assert "output truncated" in result.stdout


def test_grader_runs_setup_before_tests_and_stops_on_setup_failure() -> None:
    class RecordingSandbox:
        def __init__(self, fail_setup: bool = False) -> None:
            self.commands: list[str] = []
            self.fail_setup = fail_setup
            self.destroyed = False

        def run(self, command: str) -> CommandResult:
            self.commands.append(command)
            failed = self.fail_setup and command == "install"
            return CommandResult(
                command, 1 if failed else 0, "", "setup failed" if failed else "", False, 1
            )

        def destroy(self) -> None:
            self.destroyed = True

    sandbox = RecordingSandbox()
    result = TestGrader().grade(sandbox, ["pytest"], setup_commands=["install"])
    assert result.passed
    assert sandbox.commands == ["install", "pytest"]
    assert sandbox.destroyed

    failed_sandbox = RecordingSandbox(fail_setup=True)
    failed = TestGrader().grade(failed_sandbox, ["pytest"], setup_commands=["install"])
    assert not failed.passed
    assert failed.reason == "environment_setup_failed"
    assert failed_sandbox.commands == ["install"]
    assert failed_sandbox.destroyed
