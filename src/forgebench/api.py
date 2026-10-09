import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles

from .github import GitHubIssueClient
from .models import (
    CreateRunRequest,
    EvaluationRun,
    GitHubIssueRequest,
    HumanApprovalRequest,
    Issue,
    RunEvent,
    RunStatus,
)
from .services import RunService
from .store import ApprovalConflict, IdempotencyConflict, RunStore

service = RunService(store=RunStore(os.getenv("FORGEBENCH_DB_PATH", "forgebench.db")))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="ForgeBench", version="0.1.0", lifespan=lifespan)
workspace_root_value = os.getenv("FORGEBENCH_WORKSPACE_ROOT")
workspace_root = Path(workspace_root_value).expanduser().resolve() if workspace_root_value else None
dashboard_path = Path(__file__).resolve().parents[2] / "dashboard"
if dashboard_path.is_dir():
    assets_path = Path(__file__).resolve().parents[2] / "assets"
    if assets_path.is_dir():
        app.mount("/dashboard/assets", StaticFiles(directory=assets_path), name="dashboard-assets")
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/issues/github", response_model=Issue)
async def import_github_issue(request: GitHubIssueRequest) -> Issue:
    try:
        return await GitHubIssueClient().fetch(request.url)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/runs", response_model=list[EvaluationRun])
async def list_runs(limit: int = 100) -> list[EvaluationRun]:
    return service.store.list_runs(min(max(limit, 1), 1000))


@app.post("/runs", response_model=EvaluationRun, status_code=201)
async def create_run(request: CreateRunRequest) -> EvaluationRun:
    if not request.workspace:
        raise HTTPException(
            status_code=422, detail="workspace is required to create an executable run"
        )
    if workspace_root is None:
        raise HTTPException(
            status_code=503,
            detail="workflow execution is disabled: workspace root is not configured",
        )
    if request.config.sandbox.value != "docker":
        raise HTTPException(
            status_code=400, detail="API workflow execution requires the docker sandbox"
        )
    if request.config.require_human_approval:
        approval_token = os.getenv("FORGEBENCH_APPROVAL_TOKEN", "")
        if len(approval_token) < 32:
            raise HTTPException(
                status_code=503,
                detail=(
                    "human approval requires FORGEBENCH_APPROVAL_TOKEN with at least 32 characters"
                ),
            )
    supplied = Path(request.workspace).expanduser()
    candidate = supplied if supplied.is_absolute() else workspace_root / supplied
    workspace = candidate.resolve()
    try:
        workspace.relative_to(workspace_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="workspace must be inside the configured workspace root"
        ) from exc
    if not workspace.is_dir():
        raise HTTPException(status_code=400, detail="workspace does not exist")
    workspace_id = str(workspace.relative_to(workspace_root))

    run = EvaluationRun(
        issue=request.issue,
        config=request.config,
        benchmark_id=request.benchmark_id,
        status=RunStatus.QUEUED,
        metadata={"workspace": workspace_id},
    )
    fingerprint_data = request.model_dump(mode="json", exclude={"idempotency_key"})
    fingerprint_data["issue"].pop("id", None)
    request_hash = hashlib.sha256(
        json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    try:
        run, is_new = service.store.create_run_if_absent(
            run,
            request.idempotency_key,
            request_hash,
            RunEvent(
                run_id=run.id,
                name="run.queued",
                payload={"benchmark_id": run.benchmark_id},
            ),
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not is_new:
        return run
    return run


@app.get("/runs/{run_id}", response_model=EvaluationRun)
async def get_run(run_id: str) -> EvaluationRun:
    run = service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
async def get_run_events(run_id: str) -> list[RunEvent]:
    if service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return service.events(run_id)


@app.post("/runs/{run_id}/approval", response_model=EvaluationRun)
async def resolve_run_approval(
    run_id: str,
    request: HumanApprovalRequest,
    authorization: str | None = Header(default=None),
) -> EvaluationRun:
    token = os.getenv("FORGEBENCH_APPROVAL_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(
            status_code=503,
            detail="approval endpoint is disabled until FORGEBENCH_APPROVAL_TOKEN is configured",
        )
    if not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="invalid approval credentials")
    if service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    try:
        run = service.store.resolve_approval(
            run_id,
            request.decision,
            request.note,
        )
    except ApprovalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    service.runs[run_id] = run
    try:
        service.artifacts.record_approval(
            run,
            request.decision.value,
            request.note,
        )
    except OSError as exc:
        service.store.append_event(
            RunEvent(
                run_id=run.id,
                name="artifact.error",
                payload={"operation": "record_approval", "error_type": type(exc).__name__},
            )
        )
    return run


@app.get("/runs/{run_id}/artifact")
async def get_run_artifact(run_id: str) -> dict[str, object]:
    run = service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    artifact_path = run.metadata.get("artifact_path")
    if not artifact_path:
        return {"available": False, "files": []}
    path = Path(str(artifact_path))
    if not path.is_dir():
        return {"available": False, "files": []}
    return {
        "available": True,
        "files": sorted(str(item.relative_to(path)) for item in path.rglob("*") if item.is_file()),
    }


@app.get("/metrics")
async def get_metrics() -> dict[str, object]:
    runs = service.store.list_runs(1000)
    passed = sum(run.status == RunStatus.PASSED for run in runs)
    terminal_runs = sum(run.status in {RunStatus.PASSED, RunStatus.FAILED} for run in runs)
    queued_runs = sum(run.status == RunStatus.QUEUED for run in runs)
    running_runs = sum(run.status == RunStatus.RUNNING for run in runs)
    awaiting_approval = sum(run.status == RunStatus.AWAITING_APPROVAL for run in runs)
    failures: dict[str, int] = {}
    for run in runs:
        if run.status == RunStatus.FAILED:
            key = run.failure_category.value
            failures[key] = failures.get(key, 0) + 1
    latencies = [run.latency_ms for run in runs if run.latency_ms is not None]
    reported_costs = [run.cost_usd for run in runs if run.cost_usd is not None]
    return {
        "runs": len(runs),
        "passed": passed,
        "pass_rate": passed / terminal_runs if terminal_runs else 0.0,
        "completed_runs": terminal_runs,
        "queued_runs": queued_runs,
        "running_runs": running_runs,
        "awaiting_approval": awaiting_approval,
        "average_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "total_cost_usd": sum(reported_costs) if reported_costs else None,
        "cost_observations": len(reported_costs),
        "total_tool_calls": sum(run.tool_calls for run in runs),
        "total_input_tokens": sum(run.input_tokens for run in runs),
        "total_output_tokens": sum(run.output_tokens for run in runs),
        "failure_categories": dict(sorted(failures.items())),
    }
