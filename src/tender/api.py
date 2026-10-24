import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
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
from .store import ApprovalConflict, CancelConflict, IdempotencyConflict, RunStore

MAX_REQUEST_BYTES_DEFAULT = 1024 * 1024

service = RunService(store=RunStore(os.getenv("TENDER_DB_PATH", "tender.db")))


class RequestSizeLimitMiddleware:
    """Reject HTTP request bodies over a configured cap before routing.

    Bodies are buffered up to the cap so oversized payloads never reach
    request models. Non-HTTP scopes (lifespan, websockets, static assets) pass
    through untouched.
    """

    def __init__(self, app: Any, max_bytes: int = MAX_REQUEST_BYTES_DEFAULT) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                # An unexpected message means the body cannot be trusted to be
                # complete; never replay a partial body as if it were finished.
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body"):
                break
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # Hand control back to the real receive channel: synthesizing an
            # immediate ``http.disconnect`` makes streaming responses (for
            # example StaticFiles) abort with an empty body, because they
            # listen for disconnects while sending.
            return await receive()

        await self.app(scope, replay, send)

    async def _reject(self, send: Any) -> None:
        payload = json.dumps(
            {"detail": f"request body exceeds the {self.max_bytes} byte limit"}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


class RateLimitMiddleware:
    """Token-bucket throttle for mutating requests; disabled by default.

    ``TENDER_RATE_LIMIT_PER_MINUTE`` sets the sustained per-client rate (0 or
    unset disables the limiter) and ``TENDER_RATE_LIMIT_BURST`` the bucket
    size. Rejections return HTTP 429 with ``Retry-After``. Buckets are
    in-process and per-client-host; a distributed deployment still needs
    shared quotas.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_prune = 0.0

    @staticmethod
    def _config() -> tuple[int, int]:
        try:
            per_minute = max(0, int(os.getenv("TENDER_RATE_LIMIT_PER_MINUTE", "0")))
        except ValueError:
            per_minute = 0
        try:
            burst = int(os.getenv("TENDER_RATE_LIMIT_BURST", str(max(1, per_minute or 10))))
        except ValueError:
            burst = 10
        return per_minute, max(1, burst)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        per_minute, burst = self._config()
        if (
            scope["type"] != "http"
            or per_minute == 0
            or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            await self.app(scope, receive, send)
            return
        client = scope.get("client") or ("unknown", 0)
        key = str(client[0])
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(burst), now))
        tokens = min(float(burst), tokens + (now - last) * per_minute / 60.0)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            await self._reject(send, per_minute, tokens)
            return
        self._buckets[key] = (tokens - 1.0, now)
        self._prune(now)
        await self.app(scope, receive, send)

    def _prune(self, now: float) -> None:
        if len(self._buckets) < 1024 or now - self._last_prune < 60.0:
            return
        self._last_prune = now
        stale = now - 600.0
        self._buckets = {key: value for key, value in self._buckets.items() if value[1] >= stale}

    async def _reject(self, send: Any, per_minute: int, tokens: float) -> None:
        wait = max(1.0, (1.0 - tokens) * 60.0 / per_minute)
        payload = json.dumps({"detail": f"rate limit exceeded; retry in {wait:.0f}s"}).encode(
            "utf-8"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", f"{wait:.0f}".encode("ascii")),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


DASHBOARD_CSP = (
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
    "img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'"
)


class SecurityHeadersMiddleware:
    """Attach conservative security headers; the dashboard also gets a strict CSP."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"x-frame-options", b"DENY"))
                headers.append((b"referrer-policy", b"no-referrer"))
                if scope.get("path", "").startswith("/dashboard"):
                    headers.append((b"content-security-policy", DASHBOARD_CSP.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _require_api_token(authorization: str | None) -> None:
    """Optionally require a bearer token on mutating endpoints.

    When ``TENDER_API_TOKEN`` is unset the API stays open for local use; when it
    is set, mutating requests must present it. Read endpoints remain open so a
    local dashboard or CLI can inspect recorded runs.
    """
    token = os.getenv("TENDER_API_TOKEN", "")
    if not token:
        return
    if not hmac.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(status_code=401, detail="invalid API credentials")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(title="Tender", version=__version__, lifespan=lifespan)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_bytes=max(
        1024,
        int(os.getenv("TENDER_MAX_REQUEST_BYTES", str(MAX_REQUEST_BYTES_DEFAULT))),
    ),
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
workspace_root_value = os.getenv("TENDER_WORKSPACE_ROOT")
workspace_root = Path(workspace_root_value).expanduser().resolve() if workspace_root_value else None


def web_root() -> tuple[Path | None, Path | None]:
    """Locate the dashboard: packaged copy first, then a source checkout."""
    package = Path(__file__).resolve().parent / "web"
    if (package / "dashboard" / "index.html").is_file():
        return package / "dashboard", package / "assets"
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "dashboard" / "index.html").is_file():
        return checkout / "dashboard", checkout / "assets"
    return None, None


dashboard_path, assets_path = web_root()
if dashboard_path is not None:
    if assets_path is not None and assets_path.is_dir():
        app.mount("/dashboard/assets", StaticFiles(directory=assets_path), name="dashboard-assets")
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "name": "Tender",
        "version": app.version,
        "dashboard": "/dashboard",
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    service.store.ping()
    return {"status": "ready"}


@app.post("/issues/github", response_model=Issue)
async def import_github_issue(request: GitHubIssueRequest) -> Issue:
    try:
        return await GitHubIssueClient().fetch(request.url)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/runs", response_model=list[EvaluationRun])
async def list_runs(
    response: Response,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    status: RunStatus | None = Query(default=None),
    summary: bool = Query(
        default=False,
        description="Omit patch, test output, and issue body payloads.",
    ),
) -> list[EvaluationRun]:
    runs = service.store.list_runs(limit, offset=offset, status=status)
    response.headers["X-Total-Count"] = str(service.store.count_runs(status))
    if summary:
        runs = [
            run.model_copy(
                update={
                    "patch": None,
                    "test_output": None,
                    "issue": run.issue.model_copy(update={"body": ""}),
                }
            )
            for run in runs
        ]
    return runs


@app.post("/runs", response_model=EvaluationRun, status_code=201)
async def create_run(
    request: CreateRunRequest,
    authorization: str | None = Header(default=None),
) -> EvaluationRun:
    _require_api_token(authorization)
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
        approval_token = os.getenv("TENDER_APPROVAL_TOKEN", "")
        if len(approval_token) < 32:
            raise HTTPException(
                status_code=503,
                detail=(
                    "human approval requires TENDER_APPROVAL_TOKEN with at least 32 characters"
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
        # A replayed idempotency key returns the originally queued run.
        run, _ = service.store.create_run_if_absent(
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
    return run


@app.get("/runs/{run_id}", response_model=EvaluationRun)
async def get_run(run_id: str) -> EvaluationRun:
    run = service.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/runs/{run_id}/events", response_model=list[RunEvent])
async def get_run_events(
    run_id: str,
    limit: int = Query(default=5000, ge=1, le=20000),
    offset: int = Query(default=0, ge=0),
) -> list[RunEvent]:
    if service.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return service.store.get_events(run_id, limit=limit, offset=offset)


@app.post("/runs/{run_id}/cancel", response_model=EvaluationRun)
async def cancel_run(
    run_id: str,
    authorization: str | None = Header(default=None),
) -> EvaluationRun:
    _require_api_token(authorization)
    try:
        run = service.store.cancel_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except CancelConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    service.runs[run_id] = run
    return run


@app.post("/runs/{run_id}/approval", response_model=EvaluationRun)
async def resolve_run_approval(
    run_id: str,
    request: HumanApprovalRequest,
    authorization: str | None = Header(default=None),
) -> EvaluationRun:
    token = os.getenv("TENDER_APPROVAL_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(
            status_code=503,
            detail="approval endpoint is disabled until TENDER_APPROVAL_TOKEN is configured",
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
    """Aggregate every persisted run with SQL; the response states its coverage."""
    return service.store.aggregate_metrics()
