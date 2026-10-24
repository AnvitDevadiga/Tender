"""Regression tests for the backend-hardening pass: bounds, cancellation,
pagination, provider breadth, retry classification, legacy-payload clamping,
and worker shutdown."""

from __future__ import annotations

import json
import tomllib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tender import api as api_module
from tender.api import app
from tender.experiments import BenchmarkTask, ExperimentManifest
from tender.github import GitHubIssueClient
from tender.models import (
    ISSUE_BODY_MAX_CHARS,
    ISSUE_TITLE_MAX_CHARS,
    BenchmarkConfig,
    EvaluationRun,
    Issue,
    RunStatus,
)
from tender.providers import (
    DeterministicProvider,
    GroqProvider,
    ModelResponse,
    OpenAICompatibleProvider,
)
from tender.runtime import provider_for
from tender.services import MAX_STORED_TEST_OUTPUT_BYTES, _bounded_output
from tender.store import CancelConflict, RunStore
from tender.workflow import AgentWorkflow

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolate_api_database() -> None:
    api_module.service.store = RunStore(":memory:")
    api_module.service.runs = {}
    api_module.workspace_root = None


def make_queued_run(title: str = "Queued task") -> EvaluationRun:
    run = EvaluationRun(issue=Issue(repository="local/repo", title=title))
    api_module.service.store.create_run_if_absent(run)
    return run


def test_request_body_over_the_cap_is_rejected() -> None:
    response = client.post(
        "/runs",
        content=b"x" * (2 * 1024 * 1024),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert "byte limit" in response.json()["detail"]


def test_request_body_under_the_cap_still_reaches_routing() -> None:
    response = client.post("/runs", json={"issue": {"repository": "o/r", "title": "small"}})

    assert response.status_code == 422  # missing workspace, not a size rejection


@pytest.mark.asyncio
async def test_request_size_middleware_aborts_on_unexpected_messages() -> None:
    from tender.api import RequestSizeLimitMiddleware

    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    middleware = RequestSizeLimitMiddleware(app, max_bytes=64)
    messages = iter([{"type": "http.response.start"}])

    async def receive():
        return next(messages)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await middleware({"type": "http"}, receive, send)

    assert called is False  # a partial body is never replayed as complete
    assert sent == []


def test_root_and_ready_endpoints_report_service_state() -> None:
    assert client.get("/").json()["name"] == "Tender"
    assert client.get("/ready").json() == {"status": "ready"}


def test_queued_run_can_be_cancelled_atomically() -> None:
    run = make_queued_run()

    response = client.post(f"/runs/{run.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["metadata"]["cancelled"] is True
    events = client.get(f"/runs/{run.id}/events").json()
    assert [event["name"] for event in events] == ["run.cancelled", "run.finished"]
    assert events[-1]["payload"]["status"] == "cancelled"


def test_cancelling_twice_or_after_claim_conflicts() -> None:
    cancelled = make_queued_run("Already cancelled")
    client.post(f"/runs/{cancelled.id}/cancel")
    assert client.post(f"/runs/{cancelled.id}/cancel").status_code == 409

    claimed = make_queued_run("Claimed")
    api_module.service.store.claim_run(str(claimed.id))
    assert client.post(f"/runs/{claimed.id}/cancel").status_code == 409


def test_cancelling_unknown_run_returns_404() -> None:
    assert client.post("/runs/00000000-0000-0000-0000-000000000000/cancel").status_code == 404


def test_cancelled_runs_are_reported_separately_in_metrics() -> None:
    make_queued_run()
    cancelled = make_queued_run("Cancel me")
    api_module.service.store.cancel_run(str(cancelled.id))

    metrics = client.get("/metrics").json()

    assert metrics["cancelled_runs"] == 1
    assert metrics["queued_runs"] == 1
    assert metrics["completed_runs"] == 0
    assert metrics["pass_rate"] == 0.0


def test_run_list_filters_by_status_and_pages() -> None:
    first = make_queued_run("First")
    second = make_queued_run("Second")
    api_module.service.store.cancel_run(str(first.id))

    cancelled = client.get("/runs", params={"status": "cancelled"}).json()
    queued = client.get("/runs", params={"status": "queued"}).json()
    page = client.get("/runs", params={"limit": 1, "offset": 1}).json()

    assert [run["id"] for run in cancelled] == [str(first.id)]
    assert [run["id"] for run in queued] == [str(second.id)]
    assert len(page) == 1


def test_run_list_summary_omits_heavy_payloads() -> None:
    run = make_queued_run()
    run.issue.body = "detailed issue body"
    run.patch = "--- a/app.py\n+++ b/app.py\n"
    run.test_output = "lots of output"
    api_module.service.store.save_run(run)

    full = client.get("/runs").json()[0]
    summary = client.get("/runs", params={"summary": "true"}).json()[0]

    assert full["patch"] is not None and full["test_output"] == "lots of output"
    assert full["issue"]["body"] == "detailed issue body"
    assert "patch" in summary and summary["patch"] is None
    assert summary["test_output"] is None
    assert summary["issue"]["body"] == ""
    assert summary["issue"]["title"] == full["issue"]["title"]
    assert summary["id"] == full["id"]


def test_run_events_support_limit_and_offset() -> None:
    run = make_queued_run()
    store = api_module.service.store
    store.cancel_run(str(run.id))

    limited = client.get(f"/runs/{run.id}/events", params={"limit": 1}).json()
    offset = client.get(f"/runs/{run.id}/events", params={"limit": 5, "offset": 1}).json()

    assert [event["name"] for event in limited] == ["run.cancelled"]
    assert [event["name"] for event in offset] == ["run.finished"]


def test_worker_claim_skips_cancelled_runs() -> None:
    cancelled = make_queued_run("Cancelled first")
    surviving = make_queued_run("Claimed second")
    api_module.service.store.cancel_run(str(cancelled.id))

    claimed = api_module.service.store.claim_next_queued_run()

    assert claimed is not None
    assert claimed.id == surviving.id


def test_claim_and_cancel_race_has_exactly_one_winner(tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    store = RunStore(tmp_path / "runs.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Race"))
    store.create_run_if_absent(run)

    def cancel() -> bool:
        try:
            store.cancel_run(str(run.id))
            return True
        except CancelConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled_future = pool.submit(cancel)
        claimed = pool.submit(store.claim_next_queued_run)
        cancelled, claimed_run = cancelled_future.result(), claimed.result()

    assert cancelled != (claimed_run is not None)
    final = store.get_run(str(run.id))
    assert final is not None
    assert final.status in {RunStatus.CANCELLED, RunStatus.RUNNING}


def test_store_cancel_rejects_non_queued_runs() -> None:
    store = RunStore(":memory:")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Running"))
    store.create_run_if_absent(run)
    store.claim_run(str(run.id))

    with pytest.raises(CancelConflict, match="queued"):
        store.cancel_run(str(run.id))


def test_idempotent_replay_returns_cancelled_run_without_resurrection(tmp_path) -> None:
    store = RunStore(tmp_path / "replay.db")
    run = EvaluationRun(issue=Issue(repository="o/r", title="Replay"))
    store.create_run_if_absent(run, "replay-key", "replay-hash")
    store.cancel_run(str(run.id))

    replayed, created = store.create_run_if_absent(
        EvaluationRun(issue=Issue(repository="o/r", title="Replay")),
        "replay-key",
        "replay-hash",
    )

    assert created is False
    assert replayed.id == run.id
    assert replayed.status is RunStatus.CANCELLED


def test_api_token_guards_mutating_endpoints_when_configured(monkeypatch) -> None:
    token = "local-operator-token-that-is-long-enough"
    monkeypatch.setenv("TENDER_API_TOKEN", token)
    run = make_queued_run()

    unauthorized = client.post(f"/runs/{run.id}/cancel")
    authorized = client.post(
        f"/runs/{run.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_mutating_endpoints_stay_open_without_configured_token(monkeypatch) -> None:
    monkeypatch.delenv("TENDER_API_TOKEN", raising=False)
    run = make_queued_run()

    assert client.post(f"/runs/{run.id}/cancel").status_code == 200


def test_models_bound_issue_payloads_and_commands() -> None:
    with pytest.raises(ValidationError):
        Issue(repository="o/r", title="x" * 1025)
    with pytest.raises(ValidationError):
        Issue(repository="o/r", title="ok", base_commit="not-a-sha")
    with pytest.raises(ValidationError):
        BenchmarkConfig(commands=["true"] * 21)


def _legacy_queued_payload() -> dict:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "status": "queued",
        "benchmark_id": "legacy",
        "created_at": "2026-01-01T00:00:00+00:00",
        "issue": {
            "id": "22222222-2222-2222-2222-222222222222",
            "repository": "owner/repo",
            "title": "t" * 5000,
            "body": "b" * 300_000,
            "base_ref": "main",
            "base_commit": "abc123",
            "source_url": None,
            "number": 0,
        },
        "config": {
            "model": "legacy-model",
            "commands": [f"cmd-{index}" for index in range(25)],
            "retrieval_limit": 99,
            "timeout_seconds": 99999,
            "max_output_tokens": 1_000_000,
            "sandbox": "firecracker",
        },
    }


def _insert_raw_run(store: RunStore, payload: dict) -> None:
    with store._connect() as db:
        db.execute(
            "INSERT INTO runs (id, status, benchmark_id, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                payload["id"],
                payload["status"],
                payload["benchmark_id"],
                payload["created_at"],
                json.dumps(payload),
            ),
        )


def test_legacy_oversized_payloads_are_clamped_on_read(tmp_path) -> None:
    store = RunStore(tmp_path / "legacy.db")
    payload = _legacy_queued_payload()
    _insert_raw_run(store, payload)

    run = store.get_run(payload["id"])

    assert run is not None
    assert len(run.issue.title) == ISSUE_TITLE_MAX_CHARS
    assert len(run.issue.body) == ISSUE_BODY_MAX_CHARS
    assert run.issue.base_commit is None
    assert run.issue.number is None
    assert len(run.config.commands) == 20
    assert run.config.retrieval_limit == 50
    assert run.config.timeout_seconds == 3600
    assert run.config.max_output_tokens == 32768
    assert run.config.sandbox.value == "local"
    assert [item.id for item in store.list_runs()] == [run.id]


def test_claiming_a_legacy_run_persists_the_clamped_payload(tmp_path) -> None:
    store = RunStore(tmp_path / "legacy-claim.db")
    payload = _legacy_queued_payload()
    _insert_raw_run(store, payload)

    claimed = store.claim_next_queued_run()

    assert claimed is not None
    assert claimed.status is RunStatus.RUNNING
    with store._connect() as db:
        stored = json.loads(
            db.execute("SELECT payload FROM runs WHERE id = ?", (payload["id"],)).fetchone()[
                "payload"
            ]
        )
    assert stored["issue"]["base_commit"] is None
    assert len(stored["config"]["commands"]) == 20
    # the healed row validates strictly on the next read
    assert store.get_run(payload["id"]) is not None


def test_legacy_payload_encodings_and_empty_strings_are_repaired(tmp_path) -> None:
    store = RunStore(tmp_path / "legacy-encodings.db")
    payload = _legacy_queued_payload()
    payload["issue"]["repository"] = ""
    payload["issue"]["title"] = ""
    payload["issue"]["number"] = 0.0
    payload["config"]["model"] = ""
    payload["config"]["architecture"] = ""
    payload["config"]["retrieval_limit"] = "999"  # numeric string
    payload["config"]["max_retries"] = -3.0  # integral float
    payload["config"]["sandbox"] = {}  # unhashable legacy value
    _insert_raw_run(store, payload)

    run = store.get_run(payload["id"])

    assert run is not None
    assert run.issue.repository == "(unknown repository)"
    assert run.issue.title == "(untitled)"
    assert run.issue.number is None
    assert run.config.model == "local-deterministic"
    assert run.config.architecture == "single-agent"
    assert run.config.retrieval_limit == 50
    assert run.config.max_retries == 0
    assert run.config.sandbox.value == "local"
    assert store.claim_next_queued_run() is not None

    payload["id"] = "33333333-3333-3333-3333-333333333333"
    payload["issue"]["number"] = 9.3e18  # beyond int64 once coerced
    _insert_raw_run(store, payload)

    huge_number = store.get_run(payload["id"])
    assert huge_number is not None
    assert huge_number.issue.number is None


def test_benchmark_dataset_rejects_duplicate_task_ids(tmp_path) -> None:
    record = {
        "schema_version": 2,
        "task_id": "duplicate-1",
        "issue": {"repository": "o/r", "title": "bug"},
        "workspace": str(tmp_path),
        "base_commit": "a" * 40,
        "setup_commands": [],
        "test_commands": ["true"],
        "verification_tests": ["true"],
        "timeout_seconds": 60,
    }
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate task_id"):
        BenchmarkTask.load_jsonl(path)


def test_run_list_reports_total_count_header() -> None:
    make_queued_run("First")
    cancelled = make_queued_run("Second")
    api_module.service.store.cancel_run(str(cancelled.id))

    page = client.get("/runs", params={"limit": 1})
    filtered = client.get("/runs", params={"status": "cancelled"})

    assert page.headers["x-total-count"] == "2"
    assert len(page.json()) == 1
    assert filtered.headers["x-total-count"] == "1"


def test_mutating_routes_are_rate_limited_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("TENDER_RATE_LIMIT_PER_MINUTE", "60")
    monkeypatch.setenv("TENDER_RATE_LIMIT_BURST", "1")
    run = make_queued_run()

    first = client.post(f"/runs/{run.id}/cancel")
    second = client.post(f"/runs/{run.id}/cancel")
    read = client.get("/runs")

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    assert "rate limit exceeded" in second.json()["detail"]
    assert read.status_code == 200  # read routes stay available


def test_rate_limiting_stays_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("TENDER_RATE_LIMIT_PER_MINUTE", raising=False)
    run = make_queued_run()

    for _ in range(3):
        assert client.get("/runs").status_code == 200
    assert client.post(f"/runs/{run.id}/cancel").status_code == 200


def test_security_headers_and_dashboard_csp() -> None:
    health = client.get("/health")
    dashboard = client.get("/dashboard/")

    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" not in health.headers
    assert "default-src 'self'" in dashboard.headers["content-security-policy"]


def test_manifest_requires_identity_fields(tmp_path) -> None:
    path = tmp_path / "manifest.toml"
    path.write_text(
        """
name = "experiment"
dataset = "benchmarks/tasks.jsonl"
model = ""
architecture = "single-agent"
retrieval = "text"
repetitions = 1
max_tokens = 1024
max_minutes = 5
retry_budgets = [0]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="model"):
        ExperimentManifest.load(path)
    with path.open("rb") as handle:  # the file is valid TOML
        assert tomllib.load(handle)["model"] == ""


class _FakeGitHubResponse:
    url = "https://api.github.com/repos/owner/repo/issues/1"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self):
        return self.url

    def read(self, size):
        return self.payload[:size]


@pytest.mark.asyncio
async def test_github_import_rejects_a_response_without_a_title(monkeypatch) -> None:
    monkeypatch.setattr(
        "tender.net.open_url",
        lambda *args, **kwargs: _FakeGitHubResponse(b'{"body": "no title here"}'),
    )

    with pytest.raises(ValueError, match="missing a title"):
        await GitHubIssueClient().fetch("https://github.com/owner/repo/issues/1")


@pytest.mark.asyncio
async def test_github_import_truncates_an_oversized_body(monkeypatch) -> None:
    huge_body = "b" * (ISSUE_BODY_MAX_CHARS + 50)
    payload = json.dumps({"title": "t" * 2000, "body": huge_body}).encode("utf-8")
    monkeypatch.setattr(
        "tender.net.open_url",
        lambda *args, **kwargs: _FakeGitHubResponse(payload),
    )

    issue = await GitHubIssueClient().fetch("https://github.com/owner/repo/issues/1")

    assert len(issue.title) == ISSUE_TITLE_MAX_CHARS
    assert issue.body.endswith("...[truncated]")
    assert len(issue.body) == ISSUE_BODY_MAX_CHARS


def test_bounded_output_keeps_small_text_and_marks_large_text() -> None:
    assert _bounded_output("short") == "short"
    bounded = _bounded_output("y" * (MAX_STORED_TEST_OUTPUT_BYTES + 100))
    assert bounded.endswith("full gate output remains in run artifacts]")
    assert len(bounded.encode("utf-8")) < MAX_STORED_TEST_OUTPUT_BYTES + 200


def test_openai_compatible_provider_uses_configured_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1/")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    provider = OpenAICompatibleProvider(model="local-model")

    assert provider.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert provider.model == "local-model"


@pytest.mark.asyncio
async def test_openai_compatible_provider_json_mode_without_groq_extensions(
    monkeypatch,
) -> None:
    provider = OpenAICompatibleProvider(api_key="test-key", model="m")
    captured = {}

    def fake_request(body):
        captured.update(json.loads(body))
        return ModelResponse('{"summary":"fix"}')

    monkeypatch.setattr(provider, "_request", fake_request)
    await provider.generate(
        [{"role": "system", "content": 'Return only a JSON object with "summary".'}]
    )

    assert captured["response_format"] == {"type": "json_object"}
    assert "reasoning_format" not in captured


@pytest.mark.asyncio
async def test_openai_compatible_provider_requires_a_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAICompatibleProvider()

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not configured"):
        await provider.generate([{"role": "user", "content": "hi"}])


def test_reasoning_models_omit_temperature_and_rename_the_token_cap() -> None:
    for model in ("o3-mini", "gpt-5.1"):
        provider = OpenAICompatibleProvider(api_key="test-key", model=model)
        assert provider.supports_temperature is False
        assert provider.max_tokens_field == "max_completion_tokens"

    standard = OpenAICompatibleProvider(api_key="test-key", model="gpt-4o-mini")
    assert standard.supports_temperature is True
    assert standard.max_tokens_field == "max_tokens"


@pytest.mark.asyncio
async def test_reasoning_provider_omits_temperature_from_the_request(monkeypatch) -> None:
    provider = OpenAICompatibleProvider(api_key="test-key", model="o3-mini")
    captured: dict = {}

    def fake_request(body):
        captured.update(json.loads(body))
        return ModelResponse("ok")

    monkeypatch.setattr(provider, "_request", fake_request)
    await provider.generate([{"role": "user", "content": "hi"}])

    assert "temperature" not in captured


@pytest.mark.asyncio
async def test_provider_retries_without_temperature_when_rejected(monkeypatch) -> None:
    provider = OpenAICompatibleProvider(api_key="test-key", model="experimental-reasoner")
    bodies: list[dict] = []

    def fake_request(body):
        payload = json.loads(body)
        bodies.append(payload)
        if len(bodies) == 1:
            raise RuntimeError(
                "OpenAI-compatible API rejected the request (400): Unsupported value: "
                "'temperature' does not support 0 with this model"
            )
        return ModelResponse("ok")

    monkeypatch.setattr(provider, "_request", fake_request)

    response = await provider.generate([{"role": "user", "content": "hi"}])

    assert response.text == "ok"
    assert bodies[0]["temperature"] == 0
    assert "temperature" not in bodies[1]
    assert provider.supports_temperature is False


def test_provider_for_selects_adapters_from_the_model_prefix() -> None:
    assert isinstance(provider_for(BenchmarkConfig(model="groq/llama")), GroqProvider)
    assert isinstance(
        provider_for(BenchmarkConfig(model="openai/gpt-4o-mini")), OpenAICompatibleProvider
    )
    assert isinstance(provider_for(BenchmarkConfig()), DeterministicProvider)


@pytest.mark.asyncio
async def test_provider_retry_recovers_from_transient_server_errors(monkeypatch) -> None:
    delays: list[float] = []

    async def no_wait(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("tender.workflow.asyncio.sleep", no_wait)

    class FlakyProvider:
        calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("OpenAI-compatible API rejected the request (503): busy")
            return ModelResponse("ok")

    provider = FlakyProvider()
    result = await AgentWorkflow(provider)._generate_with_provider_retry([])

    assert result.text == "ok"
    assert provider.calls == 3
    assert delays == [2, 4]


@pytest.mark.asyncio
async def test_provider_retry_recovers_from_network_errors(monkeypatch) -> None:
    delays: list[float] = []

    async def no_wait(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("tender.workflow.asyncio.sleep", no_wait)

    class FlakyNetworkProvider:
        calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("connection timed out")
            return ModelResponse("ok")

    provider = FlakyNetworkProvider()
    result = await AgentWorkflow(provider)._generate_with_provider_retry([])

    assert result.text == "ok"
    assert delays == [2]


@pytest.mark.asyncio
async def test_provider_retry_does_not_mask_permanent_errors() -> None:
    class PermanentProvider:
        calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            raise RuntimeError("Groq API rejected the request (400): bad request")

    provider = PermanentProvider()

    with pytest.raises(RuntimeError, match="400"):
        await AgentWorkflow(provider)._generate_with_provider_retry([])
    assert provider.calls == 1


def test_second_stop_signal_restores_default_handlers(monkeypatch) -> None:
    import asyncio
    import os
    import signal

    from tender import worker as worker_module

    stopping = asyncio.Event()
    handler = worker_module._stop_handler(stopping)
    restored: list[int] = []
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_module.signal, "signal", lambda sig, action: restored.append(sig))
    monkeypatch.setattr(worker_module.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    handler()
    assert stopping.is_set()
    assert killed == []

    handler()
    assert restored == [signal.SIGINT, signal.SIGTERM]
    assert killed == [(os.getpid(), signal.SIGINT)]
