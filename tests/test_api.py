import pytest
from fastapi.testclient import TestClient

from forgebench import api as api_module
from forgebench.api import app
from forgebench.models import (
    BenchmarkConfig,
    EvaluationRun,
    FailureCategory,
    Issue,
    RunEvent,
    RunStatus,
)
from forgebench.services import RunService
from forgebench.state import WorkflowState
from forgebench.store import RunStore

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_api_database() -> None:
    api_module.service.store = RunStore(":memory:")
    api_module.service.runs = {}
    api_module.workspace_root = None


def install_fake_workflow(monkeypatch, root):
    root.mkdir(exist_ok=True)
    api_module.workspace_root = root.resolve()
    calls = []

    async def execute(self, run, workspace, *, already_claimed=False, lease_owner=None):
        calls.append(workspace)
        assert already_claimed is True
        assert lease_owner
        run.status = RunStatus.FAILED
        run.failure_category = FailureCategory.TOOLING
        self.store.append_event(RunEvent(run_id=run.id, name="workflow.started"))
        self.store.save_run(run)
        return run

    monkeypatch.setattr(RunService, "execute_workflow", execute)
    return calls


def test_health() -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_run_requires_a_repository_workspace_before_persisting() -> None:
    before = api_module.service.store.list_runs()
    response = client.post(
        "/runs",
        json={"issue": {"repository": "owner/repo", "title": "Fix bug", "body": "Reproduce it"}},
    )
    assert response.status_code == 422
    assert api_module.service.store.list_runs() == before


def test_human_approval_requires_server_token_before_reserving_run(monkeypatch) -> None:
    monkeypatch.delenv("FORGEBENCH_APPROVAL_TOKEN", raising=False)
    response = client.post(
        "/runs",
        json={
            "workspace": ".",
            "config": {"sandbox": "docker", "require_human_approval": True},
            "issue": {"repository": "local/repo", "title": "Approve me"},
        },
    )
    assert response.status_code == 503
    assert api_module.service.store.list_runs() == []


def test_final_approval_is_authenticated_atomic_and_audited(monkeypatch) -> None:
    token = "test-only-approval-token-that-is-long-enough-123"
    monkeypatch.setenv("FORGEBENCH_APPROVAL_TOKEN", token)
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Review patch"),
        config=BenchmarkConfig(require_human_approval=True),
        status=RunStatus.AWAITING_APPROVAL,
        current_state=WorkflowState.HUMAN_FINAL_APPROVAL,
    )
    api_module.service.store.create_run_if_absent(run)
    endpoint = f"/runs/{run.id}/approval"
    body = {"decision": "approve", "note": "Reviewed the verified patch."}

    unauthorized = client.post(endpoint, json=body)
    approved = client.post(
        endpoint,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    duplicate = client.post(
        endpoint,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert unauthorized.status_code == 401
    assert approved.status_code == 200
    assert approved.json()["status"] == "passed"
    assert approved.json()["current_state"] == "complete"
    assert duplicate.status_code == 409
    event_names = [event["name"] for event in client.get(f"/runs/{run.id}/events").json()]
    assert event_names[-3:] == [
        "approval.resolved",
        "workflow.state_changed",
        "workflow.finished",
    ]


def test_awaiting_approval_is_not_counted_as_a_failure(monkeypatch) -> None:
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Pending review"),
        status=RunStatus.AWAITING_APPROVAL,
        current_state=WorkflowState.HUMAN_FINAL_APPROVAL,
    )
    api_module.service.store.create_run_if_absent(run)

    metrics = client.get("/metrics").json()

    assert metrics["awaiting_approval"] == 1
    assert metrics["completed_runs"] == 0
    assert metrics["pass_rate"] == 0.0
    assert metrics["failure_categories"] == {}


def test_metrics_expose_queued_and_running_counts() -> None:
    queued = EvaluationRun(
        issue=Issue(repository="local/repo", title="Queued"), status=RunStatus.QUEUED
    )
    running = EvaluationRun(
        issue=Issue(repository="local/repo", title="Running"), status=RunStatus.RUNNING
    )
    api_module.service.store.create_run_if_absent(queued)
    api_module.service.store.create_run_if_absent(running)

    metrics = client.get("/metrics").json()

    assert metrics["queued_runs"] == 1
    assert metrics["running_runs"] == 1
    assert metrics["completed_runs"] == 0


def test_workflow_api_is_disabled_without_configured_workspace_root() -> None:
    api_module.workspace_root = None
    response = client.post(
        "/runs",
        json={
            "workspace": ".",
            "issue": {"repository": "local/repo", "title": "Fix bug"},
            "config": {"sandbox": "docker"},
        },
    )
    assert response.status_code == 503


def test_rejected_workflow_request_does_not_reserve_idempotency_key(tmp_path, monkeypatch) -> None:
    api_module.workspace_root = None
    key = "rejected-workflow-does-not-reserve-key"
    body = {
        "idempotency_key": key,
        "workspace": ".",
        "issue": {"repository": "local/repo", "title": "Fix bug"},
        "config": {"sandbox": "docker"},
    }
    rejected = client.post("/runs", json=body)
    assert rejected.status_code == 503

    install_fake_workflow(monkeypatch, tmp_path)
    accepted = client.post("/runs", json=body)
    assert accepted.status_code == 201


def test_workflow_api_rejects_non_docker_execution(tmp_path) -> None:
    api_module.workspace_root = tmp_path.resolve()
    response = client.post(
        "/runs",
        json={
            "workspace": ".",
            "issue": {"repository": "local/repo", "title": "Fix bug"},
            "config": {"sandbox": "local"},
        },
    )
    assert response.status_code == 400


def test_workflow_api_rejects_workspace_path_escape(tmp_path) -> None:
    api_module.workspace_root = tmp_path.resolve()
    outside = tmp_path.parent / "outside-workspace"
    outside.mkdir(exist_ok=True)
    response = client.post(
        "/runs",
        json={
            "workspace": str(outside),
            "issue": {"repository": "local/repo", "title": "Fix bug"},
            "config": {"sandbox": "docker"},
        },
    )
    assert response.status_code == 400


def test_idempotency_key_returns_original_run_without_second_execution(
    tmp_path, monkeypatch
) -> None:
    calls = install_fake_workflow(monkeypatch, tmp_path)
    body = {
        "idempotency_key": "api-test-idempotency-key",
        "workspace": ".",
        "config": {"sandbox": "docker"},
        "issue": {"repository": "owner/repo", "title": "Stable issue", "body": "same request"},
    }
    first = client.post("/runs", json=body)
    second = client.post("/runs", json=body)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "queued"
    assert client.get(f"/runs/{first.json()['id']}").json()["status"] == "queued"
    assert len(calls) == 0
    stored = api_module.service.store.get_run(first.json()["id"])
    assert stored.metadata["workspace"] == "."
    events = client.get(f"/runs/{first.json()['id']}/events").json()
    assert [event["name"] for event in events] == ["run.queued"]


def test_worker_claims_queued_run_and_executes_it(tmp_path, monkeypatch) -> None:
    import asyncio

    from forgebench.worker import run_one

    install_fake_workflow(monkeypatch, tmp_path)
    response = client.post(
        "/runs",
        json={
            "workspace": ".",
            "config": {"sandbox": "docker"},
            "issue": {"repository": "owner/repo", "title": "Queued task"},
        },
    )
    run_id = response.json()["id"]

    processed = asyncio.run(run_one(api_module.service.store, tmp_path))

    assert processed is not None
    assert str(processed.id) == run_id
    assert processed.status is RunStatus.FAILED
    events = api_module.service.store.get_events(run_id)
    assert [event.name for event in events] == [
        "run.queued",
        "worker.claimed",
        "workflow.started",
    ]


def test_worker_heartbeat_keeps_long_running_claim_live(tmp_path, monkeypatch) -> None:
    import asyncio

    from forgebench.worker import run_one

    tmp_path.mkdir(exist_ok=True)
    api_module.workspace_root = tmp_path.resolve()

    async def slow_execute(self, run, workspace, *, already_claimed=False, lease_owner=None):
        assert already_claimed
        assert lease_owner
        await asyncio.sleep(1.2)
        return run

    monkeypatch.setattr(RunService, "execute_workflow", slow_execute)
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Long task"),
        status=RunStatus.QUEUED,
        metadata={"workspace": "."},
    )
    api_module.service.store.create_run_if_absent(run)

    processed = asyncio.run(run_one(api_module.service.store, tmp_path, lease_seconds=2))

    assert processed is not None
    assert processed.status is RunStatus.RUNNING
    assert api_module.service.store.recover_interrupted_runs() == []


def test_worker_rejects_queued_workspace_outside_its_root(tmp_path) -> None:
    import asyncio

    from forgebench.worker import run_one

    outside = tmp_path.parent / "outside-forgebench"
    outside.mkdir(exist_ok=True)
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Path escape"),
        status=RunStatus.QUEUED,
        metadata={"workspace": str(outside)},
    )
    api_module.service.store.create_run_if_absent(run)

    processed = asyncio.run(run_one(api_module.service.store, tmp_path))

    assert processed is not None
    assert processed.status is RunStatus.FAILED
    assert processed.failure_category is FailureCategory.ENVIRONMENT
    assert "worker_workspace_error" in processed.test_output


def test_idempotency_key_rejects_different_request_body(tmp_path, monkeypatch) -> None:
    install_fake_workflow(monkeypatch, tmp_path)
    key = "api-test-idempotency-conflict"
    first = client.post(
        "/runs",
        json={
            "idempotency_key": key,
            "workspace": ".",
            "config": {"sandbox": "docker"},
            "issue": {"repository": "o/r", "title": "first"},
        },
    )
    second = client.post(
        "/runs",
        json={
            "idempotency_key": key,
            "workspace": ".",
            "config": {"sandbox": "docker"},
            "issue": {"repository": "o/r", "title": "second"},
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
