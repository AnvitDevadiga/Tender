from tender.models import EvaluationRun, Issue
from tender.store import RunStore


def test_store_lists_newest_runs(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    first = EvaluationRun(issue=Issue(repository="o/r", title="first"))
    second = EvaluationRun(issue=Issue(repository="o/r", title="second"))
    store.save_run(first)
    store.save_run(second)
    assert {run.issue.title for run in store.list_runs()} == {"first", "second"}
