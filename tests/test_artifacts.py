import json

from tender.artifacts import ArtifactStore
from tender.models import EvaluationRun, FailureCategory, Issue, RunStatus
from tender.workflow import WorkflowResult


def test_artifact_store_writes_portable_run_directory(tmp_path) -> None:
    run = EvaluationRun(issue=Issue(repository="o/r", title="bug"))
    result = WorkflowResult(False, (), run.failure_category)
    path = ArtifactStore(tmp_path).write(run, result)
    assert (path / "run.json").is_file()
    assert json.loads((path / "result.json").read_text())["passed"] is False


def test_artifact_store_updates_final_independent_verification_result(tmp_path) -> None:
    run = EvaluationRun(issue=Issue(repository="o/r", title="bug"), status=RunStatus.PASSED)
    store = ArtifactStore(tmp_path)
    path = store.write(run, WorkflowResult(True, (), run.failure_category))
    run.status = RunStatus.FAILED
    run.failure_category = FailureCategory.TEST
    checks = [{"command": "verify", "passed": False, "return_code": 1}]

    store.record_verification(run, checks)

    result = json.loads((path / "result.json").read_text(encoding="utf-8"))
    verification = json.loads((path / "verification.json").read_text(encoding="utf-8"))
    persisted_run = json.loads((path / "run.json").read_text(encoding="utf-8"))
    assert result["passed"] is False
    assert result["verification"]["checks"] == checks
    assert verification["passed"] is False
    assert persisted_run["status"] == "failed"
