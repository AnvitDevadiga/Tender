from __future__ import annotations

import asyncio
import os
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from .artifacts import ArtifactStore
from .models import EvaluationRun, FailureCategory, RunEvent, RunStatus
from .runtime import provider_for
from .services import RunService
from .state import WorkflowState
from .store import LeaseLost, RunStore


def _workspace_for(run: EvaluationRun, root: Path) -> Path:
    value = run.metadata.get("workspace")
    if not isinstance(value, str) or not value:
        raise ValueError("queued run has no workspace")
    supplied = Path(value).expanduser()
    workspace = (supplied if supplied.is_absolute() else root / supplied).resolve()
    workspace.relative_to(root)
    if not workspace.is_dir():
        raise ValueError("queued workspace no longer exists")
    return workspace


async def run_one(
    store: RunStore,
    workspace_root: str | Path,
    artifact_root: str | Path = "artifacts",
    worker_id: str | None = None,
    lease_seconds: int = 90,
) -> EvaluationRun | None:
    """Claim and execute the oldest queued run, returning None when the queue is empty."""
    owner = worker_id or str(uuid4())
    run = store.claim_next_queued_run(owner, lease_seconds)
    if run is None:
        return None
    stopping = Event()

    def renew_lease() -> None:
        while not stopping.wait(max(1, lease_seconds // 3)):
            try:
                if not store.heartbeat_run(str(run.id), owner, lease_seconds):
                    return
            except Exception:
                # A transient database error is retried; recovery will only act
                # after the last lease has actually expired.
                continue

    heartbeat = Thread(target=renew_lease, name=f"lease-{run.id}", daemon=True)
    heartbeat.start()
    try:
        root = Path(workspace_root).expanduser().resolve()
        try:
            workspace = _workspace_for(run, root)
        except (OSError, ValueError) as exc:
            run.status = RunStatus.FAILED
            run.failure_category = FailureCategory.ENVIRONMENT
            run.current_state = WorkflowState.FAILED
            run.test_output = f"worker_workspace_error: {type(exc).__name__}: {str(exc)[:300]}"
            try:
                store.save_run_with_event(
                    run,
                    RunEvent(
                        run_id=run.id,
                        name="workflow.state_changed",
                        payload={
                            "from": None,
                            "to": WorkflowState.FAILED.value,
                            "summary": "Worker rejected the queued workspace.",
                        },
                    ),
                    lease_owner=owner,
                )
            except LeaseLost:
                return store.get_run(str(run.id)) or run
            store.append_event(
                RunEvent(
                    run_id=run.id,
                    name="workflow.finished",
                    payload={
                        "status": run.status.value,
                        "failure_category": run.failure_category.value,
                    },
                )
            )
            return run

        service = RunService(
            provider=provider_for(run.config),
            store=store,
            artifacts=ArtifactStore(artifact_root),
        )
        return await service.execute_workflow(
            run, str(workspace), already_claimed=True, lease_owner=owner
        )
    finally:
        stopping.set()
        heartbeat.join(timeout=2)


async def serve() -> None:
    db_path = os.getenv("FORGEBENCH_DB_PATH", "forgebench.db")
    root_value = os.getenv("FORGEBENCH_WORKSPACE_ROOT")
    if not root_value:
        raise RuntimeError("FORGEBENCH_WORKSPACE_ROOT is required for worker execution")
    poll_seconds = max(0.1, min(float(os.getenv("FORGEBENCH_WORKER_POLL_SECONDS", "1")), 30.0))
    lease_seconds = max(10, min(int(os.getenv("FORGEBENCH_WORKER_LEASE_SECONDS", "90")), 3600))
    store = RunStore(db_path)
    worker_id = str(uuid4())
    while True:
        store.recover_interrupted_runs()
        processed = await run_one(
            store,
            root_value,
            os.getenv("FORGEBENCH_ARTIFACT_ROOT", "artifacts"),
            worker_id,
            lease_seconds,
        )
        if processed is None:
            await asyncio.sleep(poll_seconds)
