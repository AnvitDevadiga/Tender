import subprocess


def test_fixture_tasks_have_pinned_commits() -> None:
    from forgebench.experiments import BenchmarkTask

    tasks = BenchmarkTask.load_jsonl("benchmarks/tasks.jsonl")
    for task in tasks:
        assert task.issue.base_commit
        result = subprocess.run(
            ["git", "cat-file", "-e", task.issue.base_commit], cwd=task.workspace
        )
        assert result.returncode == 0
