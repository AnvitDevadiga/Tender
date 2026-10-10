import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tender.models import EvaluationRun, Issue, RunEvent, RunStatus
from tender.state import WorkflowState
from tender.store import LeaseLost, RunStore


def test_store_round_trip_and_append_only_events(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Bug"), benchmark_id="dev-v1")
    store.save_run(run)
    store.append_event(RunEvent(run_id=run.id, name="run.created"))

    loaded = store.get_run(str(run.id))
    assert loaded is not None
    assert loaded.benchmark_id == "dev-v1"
    assert [event.name for event in store.get_events(str(run.id))] == ["run.created"]


def test_run_claim_is_atomic_and_only_succeeds_once(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Bug"))
    store.create_run_if_absent(run)

    first_claim = store.claim_run(str(run.id))
    second_claim = store.claim_run(str(run.id))

    assert first_claim is not None
    assert first_claim.status.value == "running"
    assert second_claim is None
    assert store.get_run(str(run.id)).status.value == "running"


def test_worker_claims_oldest_queued_run_once(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    first = EvaluationRun(issue=Issue(repository="o/r", title="First"))
    second = EvaluationRun(issue=Issue(repository="o/r", title="Second"))
    store.create_run_if_absent(first)
    store.create_run_if_absent(second)

    claimed_first = store.claim_next_queued_run()
    claimed_second = store.claim_next_queued_run()
    no_more = store.claim_next_queued_run()

    assert claimed_first is not None and str(claimed_first.id) == str(first.id)
    assert claimed_second is not None and str(claimed_second.id) == str(second.id)
    assert claimed_first.status.value == "running"
    assert claimed_second.status.value == "running"
    assert store.heartbeat_run(str(first.id), "worker") is True
    assert no_more is None


def test_concurrent_workers_claim_distinct_queued_runs(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    first = EvaluationRun(issue=Issue(repository="o/r", title="First"))
    second = EvaluationRun(issue=Issue(repository="o/r", title="Second"))
    store.create_run_if_absent(first)
    store.create_run_if_absent(second)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(
            pool.map(
                lambda worker_id: store.claim_next_queued_run(worker_id),
                ("worker-a", "worker-b"),
            )
        )

    assert {str(run.id) for run in claimed if run is not None} == {
        str(first.id),
        str(second.id),
    }


def test_live_lease_renews_and_is_not_recovered(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Active"))
    store.create_run_if_absent(run)
    claimed = store.claim_next_queued_run("worker-a", lease_seconds=60)
    assert claimed is not None

    assert store.heartbeat_run(str(run.id), "worker-a", lease_seconds=60) is True
    assert store.heartbeat_run(str(run.id), "worker-b", lease_seconds=60) is False
    assert store.recover_interrupted_runs() == []
    assert store.get_run(str(run.id)).status.value == "running"


def test_expired_lease_is_failed_once_and_old_worker_is_fenced(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Abandoned"))
    store.create_run_if_absent(run)
    claimed = store.claim_next_queued_run("worker-a", lease_seconds=60)
    assert claimed is not None
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE runs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", str(run.id)),
        )

    recovered = store.recover_interrupted_runs()

    assert [item.id for item in recovered] == [run.id]
    assert recovered[0].metadata["recovery_policy"] == "failed_without_replay"
    assert store.recover_interrupted_runs() == []
    assert store.heartbeat_run(str(run.id), "worker-a") is False
    recovered[0].status = RunStatus.RUNNING
    with pytest.raises(LeaseLost):
        store.save_run(recovered[0], lease_owner="worker-a")


def test_existing_sqlite_runs_table_gets_lease_columns_migrated(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
            "benchmark_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
        )

    RunStore(path)

    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    assert {"lease_owner", "lease_expires_at", "heartbeat_at"}.issubset(columns)


def test_created_event_is_atomic_with_run_enqueue(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Queued"))
    event = RunEvent(run_id=run.id, name="run.queued")

    created, is_new = store.create_run_if_absent(run, created_event=event)

    assert is_new is True
    assert created.id == run.id
    assert [item.name for item in store.get_events(str(run.id))] == ["run.queued"]


def test_interrupted_runs_are_failed_without_automatic_replay(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Interrupted"))
    store.create_run_if_absent(run)
    assert store.claim_run(str(run.id)) is not None
    claimed = store.get_run(str(run.id))
    claimed.current_state = WorkflowState.TEST
    store.save_run(claimed)

    recovered = store.recover_interrupted_runs()

    assert len(recovered) == 1
    assert recovered[0].status.value == "failed"
    assert recovered[0].metadata["recovery_policy"] == "failed_without_replay"
    assert recovered[0].metadata["interrupted_at_state"] == "test"
    assert recovered[0].current_state is WorkflowState.FAILED
    assert [event.name for event in store.get_events(str(run.id))] == [
        "run.interrupted",
        "run.finished",
    ]
    assert store.recover_interrupted_runs() == []


def test_state_transition_and_event_persist_atomically(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="State update"))
    store.create_run_if_absent(run)
    run.current_state = WorkflowState.TASK_RECEIVED
    event = RunEvent(
        run_id=run.id,
        name="workflow.state_changed",
        payload={"from": None, "to": "task_received"},
    )

    store.save_run_with_event(run, event)

    assert store.get_run(str(run.id)).current_state is WorkflowState.TASK_RECEIVED
    assert store.get_events(str(run.id))[0].payload["to"] == "task_received"


def test_idempotency_key_is_bound_to_request_fingerprint(tmp_path) -> None:
    import pytest

    from tender.store import IdempotencyConflict

    store = RunStore(tmp_path / "runs.db")
    first = EvaluationRun(issue=Issue(repository="o/r", title="Bug"))
    created, is_new = store.create_run_if_absent(first, "request-1", "hash-a")
    duplicate, duplicate_new = store.create_run_if_absent(
        EvaluationRun(issue=Issue(repository="o/r", title="Bug")), "request-1", "hash-a"
    )

    assert is_new and not duplicate_new
    assert duplicate.id == created.id
    with pytest.raises(IdempotencyConflict):
        store.create_run_if_absent(
            EvaluationRun(issue=Issue(repository="o/r", title="Other")), "request-1", "hash-b"
        )
