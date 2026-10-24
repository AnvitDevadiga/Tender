"""Phase 2 tests: leases, metrics, deadlines, evidence, timing, and experiment quality."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tender.api as api_module
from tender.artifacts import ArtifactStore
from tender.evidence import sanitize_text, verify_bundle
from tender.experiments import BenchmarkTask, ExperimentManifest
from tender.models import (
    BenchmarkConfig,
    EvaluationRun,
    FailureCategory,
    Issue,
    RunEvent,
    RunStatus,
    utcnow,
)
from tender.outcomes import CheckOutcome, OverallStatus
from tender.providers import ModelResponse
from tender.runner import (
    LiveProviderNotAuthorized,
    authorize_live_run,
    compare_experiment_results,
    plan_trials,
    run_experiment_sync,
    sign_test_p_value,
    wilson_interval,
)
from tender.sandbox import ExecutionBudget, Sandbox, current_budget
from tender.services import RunService
from tender.store import LeaseLost, RunStore
from tender.workflow import AgentWorkflow

PY = sys.executable
PLAN = '{"summary":"Fix it","steps":["Edit app.py"],"target_files":["app.py"]}'
FIX = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n"
EXPECT_TWO = f"{PY} -c \"assert open('app.py').read().strip() == 'return 2'\""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True)
    (path / "app.py").write_text("return 1\n", encoding="utf-8")
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Tender Test")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD")


class FixProvider:
    def __init__(self, on_generate=None) -> None:
        self.calls = 0
        self.on_generate = on_generate

    async def generate(self, messages, tools=None):
        self.calls += 1
        if self.on_generate:
            self.on_generate()
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(PLAN, input_tokens=3, output_tokens=4)
        return ModelResponse(FIX, input_tokens=5, output_tokens=6)


def expire_lease(store: RunStore, run_id: str) -> None:
    past = (utcnow() - timedelta(seconds=30)).isoformat()
    with store._connect() as db:
        db.execute("UPDATE runs SET lease_expires_at = ? WHERE id = ?", (past, run_id))


def queued_run(store: RunStore, **issue) -> EvaluationRun:
    run = EvaluationRun(
        issue=Issue(repository="o/r", title="fix", body="fix", **issue),
        config=BenchmarkConfig(commands=[EXPECT_TWO], max_retries=0),
    )
    store.create_run_if_absent(run)
    return run


# ------------------------------------------------------------------ 2.1 leases


def test_an_expired_lease_is_never_renewed(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = queued_run(store)
    claimed = store.claim_next_queued_run("worker-a", lease_seconds=60)
    assert claimed is not None
    assert store.heartbeat_run(str(run.id), "worker-a") is True
    expire_lease(store, str(run.id))
    assert store.heartbeat_run(str(run.id), "worker-a") is False


def test_stale_worker_cannot_append_events_after_recovery(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = queued_run(store)
    store.claim_next_queued_run("worker-a", lease_seconds=60)
    store.append_event(RunEvent(run_id=run.id, name="live"), lease_owner="worker-a")
    expire_lease(store, str(run.id))
    recovered = store.recover_interrupted_runs()
    assert [str(item.id) for item in recovered] == [str(run.id)]
    before = len(store.get_events(str(run.id)))

    with pytest.raises(LeaseLost):
        store.append_event(RunEvent(run_id=run.id, name="stale"), lease_owner="worker-a")
    with pytest.raises(LeaseLost):
        store.finalize_run(store.get_run(str(run.id)), [], lease_owner="worker-a")
    assert len(store.get_events(str(run.id))) == before
    assert store.get_run(str(run.id)).status is RunStatus.FAILED


@pytest.mark.asyncio
async def test_stale_worker_cannot_publish_artifacts_or_results(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    store = RunStore(tmp_path / "runs.db")
    run = queued_run(store)
    claimed = store.claim_next_queued_run("worker-a", lease_seconds=60)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    provider = FixProvider(on_generate=lambda: expire_lease(store, str(run.id)))

    result = await RunService(provider, store, artifacts).execute_workflow(
        claimed, str(source), already_claimed=True, lease_owner="worker-a"
    )

    assert result.status is RunStatus.RUNNING  # never finalized by the stale owner
    assert not (tmp_path / "artifacts" / str(run.id)).exists()
    staging = tmp_path / "artifacts" / ".staging"
    assert not staging.exists() or not any(staging.iterdir())
    names = [event.name for event in store.get_events(str(run.id))]
    assert "workflow.finished" not in names and "attempt.finished" not in names
    store.recover_interrupted_runs()
    assert store.get_run(str(run.id)).status is RunStatus.FAILED


def test_concurrent_claims_have_exactly_one_winner(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    queued_run(store)
    winners: list[object] = []
    barrier = threading.Barrier(8)

    def claim(index: int) -> None:
        barrier.wait()
        claimed = store.claim_next_queued_run(f"worker-{index}", lease_seconds=60)
        if claimed is not None:
            winners.append(claimed)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1


def test_recovery_never_touches_a_finished_accepted_run(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    run = queued_run(store)
    claimed = store.claim_next_queued_run("worker-a", lease_seconds=60)
    claimed.status = RunStatus.PASSED
    store.finalize_run(claimed, [RunEvent(run_id=run.id, name="done")], lease_owner="worker-a")
    assert store.recover_interrupted_runs() == []
    assert store.get_run(str(run.id)).status is RunStatus.PASSED
    with pytest.raises(LeaseLost):
        store.save_run(claimed, lease_owner="worker-a")


# ------------------------------------------------------------------ 2.2 metrics


def bulk_runs(store: RunStore, count: int, *, provider_failures: int = 0) -> list[EvaluationRun]:
    runs = []
    for index in range(count):
        status = RunStatus.PASSED if index % 3 == 0 else RunStatus.FAILED
        run = EvaluationRun(
            issue=Issue(repository="o/r", title=f"task {index}"),
            config=BenchmarkConfig(model="model-a" if index % 2 else "model-b"),
            benchmark_id="bench",
            status=status,
            latency_ms=index + 1,
            cost_usd=0.01 if index % 4 == 0 else None,
            input_tokens=10,
            output_tokens=2,
            tool_calls=3,
        )
        if status is RunStatus.FAILED:
            run.failure_category = (
                FailureCategory.PROVIDER if index < provider_failures * 3 else FailureCategory.TEST
            )
        runs.append(run)
    with store._connect() as db:
        db.executemany(
            "INSERT INTO runs (id, status, benchmark_id, created_at, payload) VALUES (?,?,?,?,?)",
            [
                (
                    str(r.id),
                    r.status.value,
                    r.benchmark_id,
                    r.created_at.isoformat(),
                    r.model_dump_json(),
                )
                for r in runs
            ],
        )
    return runs


def percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@pytest.mark.parametrize("count", [0, 1, 1000, 1001, 2500])
def test_metrics_cover_every_run_without_truncation(count, monkeypatch) -> None:
    store = RunStore(":memory:")
    runs = bulk_runs(store, count, provider_failures=count // 10)
    monkeypatch.setattr(api_module.service, "store", store)
    metrics = TestClient(api_module.app).get("/metrics").json()

    passed = sum(run.status is RunStatus.PASSED for run in runs)
    provider = sum(run.failure_category.value == "provider_error" for run in runs)
    latencies = [run.latency_ms for run in runs]
    costs = [run.cost_usd for run in runs if run.cost_usd is not None]
    assert metrics["runs"] == count
    assert metrics["coverage"] == {
        "scope": "all_runs",
        "runs_counted": count,
        "total_runs": count,
        "truncated": False,
    }
    assert metrics["passed"] == passed
    assert metrics["provider_failures"] == provider
    assert metrics["total_input_tokens"] == 10 * count
    assert metrics["total_tool_calls"] == 3 * count
    assert metrics["cost_observations"] == len(costs)
    if count:
        assert metrics["average_latency_ms"] == pytest.approx(sum(latencies) / count)
        assert metrics["latency_p50_ms"] == pytest.approx(round(percentile(latencies, 0.5), 1))
        assert metrics["latency_p95_ms"] == pytest.approx(round(percentile(latencies, 0.95), 1))
        assert metrics["total_cost_usd"] == pytest.approx(sum(costs))
        terminal = count
        assert metrics["pass_rate"] == pytest.approx(passed / terminal)
        if terminal > provider:
            assert metrics["pass_rate_excluding_provider_failures"] == pytest.approx(
                passed / (terminal - provider)
            )
        assert sum(item["runs"] for item in metrics["by_model"].values()) == count
    else:
        assert metrics["average_latency_ms"] is None
        assert metrics["latency_p50_ms"] is None
        assert metrics["total_cost_usd"] is None
        assert metrics["pass_rate_excluding_provider_failures"] is None


def test_paginated_listing_and_metrics_agree_on_large_datasets(monkeypatch) -> None:
    store = RunStore(":memory:")
    bulk_runs(store, 2100)
    monkeypatch.setattr(api_module.service, "store", store)
    client = TestClient(api_module.app)
    seen = set()
    for offset in range(0, 2100, 1000):
        page = client.get("/runs", params={"limit": 1000, "offset": offset, "summary": "true"})
        assert page.headers["X-Total-Count"] == "2100"
        seen.update(item["id"] for item in page.json())
    assert len(seen) == 2100 == client.get("/metrics").json()["runs"]


# ------------------------------------------------------- 2.4/2.5 time + budgets


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_pid(path: Path, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.02)
    raise AssertionError(f"process never wrote {path}")


def test_command_timeout_kills_a_long_running_process(tmp_path) -> None:
    started = time.monotonic()
    result = Sandbox(tmp_path, timeout_seconds=1).run(f"echo $$ > {tmp_path}/pid; exec sleep 30")
    assert result.timed_out is True and result.passed is False
    assert time.monotonic() - started < 5
    assert not pid_alive(int((tmp_path / "pid").read_text()))


def test_run_budget_overrides_a_longer_command_timeout(tmp_path) -> None:
    token = current_budget.set(ExecutionBudget(1))
    try:
        started = time.monotonic()
        result = Sandbox(tmp_path, timeout_seconds=60).run("sleep 30")
    finally:
        current_budget.reset(token)
    assert result.timed_out is True
    assert time.monotonic() - started < 5


def test_cancellation_kills_the_process_group_including_children(tmp_path) -> None:
    budget = ExecutionBudget()
    script = f"sleep 30 & echo $! > {tmp_path}/child; echo $$ > {tmp_path}/pid; wait"
    outcome: dict[str, object] = {}

    def target() -> None:
        token = current_budget.set(budget)
        try:
            outcome["result"] = Sandbox(tmp_path, timeout_seconds=60).run(script)
        finally:
            current_budget.reset(token)

    thread = threading.Thread(target=target)
    thread.start()
    child = wait_for_pid(tmp_path / "child")
    parent = wait_for_pid(tmp_path / "pid")
    budget.cancel()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert outcome["result"].timed_out is True
    time.sleep(0.1)
    assert not pid_alive(parent) and not pid_alive(child)
    assert budget.live_processes == 0


@pytest.mark.asyncio
async def test_cancelling_a_workflow_stops_its_subprocesses(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    pidfile = tmp_path / "pid"
    workflow = AgentWorkflow(FixProvider())
    task = asyncio.create_task(
        workflow.run(
            Issue(repository="o/r", title="fix", body="fix"),
            agent,
            [f"echo $$ > {pidfile}; exec sleep 30"],
            max_retries=0,
            verification_workspace=source,
        )
    )
    pid = await asyncio.to_thread(wait_for_pid, pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        if not pid_alive(pid):
            break
        await asyncio.sleep(0.05)
    assert not pid_alive(pid)


@pytest.mark.asyncio
async def test_workflow_deadline_ends_the_run_as_a_timeout(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    started = time.monotonic()

    result = await AgentWorkflow(FixProvider()).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        ["sleep 30"],
        max_retries=3,
        verification_workspace=source,
        deadline_seconds=1,
    )

    assert time.monotonic() - started < 10
    assert result.passed is False
    assert result.failure_category.value == "timeout"
    assert len(result.attempts) == 1  # a budget timeout is not retried


@pytest.mark.asyncio
async def test_verification_timeout_is_inconclusive_not_a_coding_failure(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)

    result = await AgentWorkflow(FixProvider()).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        timeout_seconds=1,
        verification_commands=["sleep 30"],
        verification_workspace=source,
    )

    assert result.outcome.target is CheckOutcome.ERROR
    assert result.outcome.timed_out is True
    assert result.outcome.overall is OverallStatus.INCONCLUSIVE


@pytest.mark.asyncio
async def test_stage_timings_and_retry_counters_are_recorded(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)

    class Flaky(FixProvider):
        plans = 0
        transient = True

        async def generate(self, messages, tools=None):
            if self.transient:
                self.transient = False
                raise RuntimeError("Groq API rejected the request (503): Please try again in 1ms")
            if "implementation plan" in messages[0]["content"].lower():
                self.plans += 1
                if self.plans == 1:
                    return ModelResponse("not json")
            return await super().generate(messages, tools)

    result = await AgentWorkflow(Flaky()).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        verification_workspace=source,
    )

    assert result.passed is True
    for stage in (
        "environment_setup",
        "repository_analysis",
        "retrieval",
        "provider_calls",
        "agent_tests",
        "static_analysis",
        "target_verification",
        "total_wall_clock",
    ):
        assert stage in result.timing_ms, stage
    assert "regression_verification" not in result.timing_ms  # not run, so not zero-filled
    assert result.counters["provider_transport_retries"] == 1
    assert result.counters["planning_format_retries"] == 1
    assert result.counters["attempts"] == 1


# ------------------------------------------------------------- 2.6 evidence


@pytest.mark.asyncio
async def test_evidence_bundle_hashes_reconcile_and_detect_tampering(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TENDER_TEST_SECRET", "sk-thisisnotarealkeybutlookslikeone123")
    source = tmp_path / "source"
    commit = make_repo(source)
    store = RunStore(tmp_path / "runs.db")
    service = RunService(FixProvider(), store, ArtifactStore(tmp_path / "artifacts"))
    run = EvaluationRun(
        issue=Issue(repository="o/r", title="fix", body="fix", base_commit=commit),
        config=BenchmarkConfig(commands=[EXPECT_TWO], max_retries=0),
    )

    finished = await service.execute_workflow(run, str(source))

    bundle = tmp_path / "artifacts" / str(run.id) / "evidence"
    assert finished.status is RunStatus.PASSED
    assert verify_bundle(bundle) == []
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["signed"] is False
    kinds = {entry["path"]: entry["kind"] for entry in manifest["files"]}
    assert kinds["source/events.jsonl"] == "source"
    assert kinds["derived/summary.json"] == "derived"
    repository = json.loads((bundle / "source" / "repository.json").read_text())
    assert repository["executed_base_commit"] == repository["verification_commit"] == commit
    patch = (bundle / "source" / "attempts" / "1" / "patch.diff").read_bytes()
    import hashlib

    assert hashlib.sha256(patch).hexdigest() == manifest["final_patch_sha256"]
    events = (bundle / "source" / "events.jsonl").read_text().splitlines()
    names = [json.loads(line)["name"] for line in events]
    assert names[-1] == "workflow.finished" and "task_verification.completed" in names
    blob = "".join(path.read_text() for path in bundle.rglob("*") if path.is_file())
    assert "sk-thisisnotarealkey" not in blob and "TENDER_TEST_SECRET" not in blob
    assert str(Path.home()) not in blob

    (bundle / "source" / "task.json").write_text("{}")
    (bundle / "extra.txt").write_text("unlisted")
    problems = verify_bundle(bundle)
    assert "hash mismatch: source/task.json" in problems
    assert "unlisted file: extra.txt" in problems


def test_sanitizer_redacts_secret_patterns_and_host_paths() -> None:
    text = sanitize_text(
        f"key=gsk_abcdefghijklmnop token ghp_abcdefghijklmnopqrstu "
        f"Authorization: Bearer abc.def.ghijkl password = hunter22 at {Path.home()}/repo"
    )
    assert "gsk_abcdefghijklmnop" not in text
    assert "ghp_abcdefghijklmnopqrstu" not in text
    assert "abc.def.ghijkl" not in text
    assert "hunter22" not in text
    assert str(Path.home()) not in text and "~/repo" in text


# ---------------------------------------------------------- 2.8 packaging


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build the wheel")
def test_built_wheel_contains_the_dashboard(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        ["uv", "build", "--wheel", "-q", "-o", str(tmp_path)], cwd=root, check=True, timeout=120
    )
    wheel = next(tmp_path.glob("tender-*.whl"))
    import zipfile

    names = set(zipfile.ZipFile(wheel).namelist())
    for required in (
        "tender/web/dashboard/index.html",
        "tender/web/dashboard/app.js",
        "tender/web/dashboard/styles.css",
        "tender/web/assets/favicon.svg",
    ):
        assert required in names


# ------------------------------------------------------ 2.9 experiment quality


def manifest_file(tmp_path: Path, **extra) -> Path:
    fields = {
        "name": '"quality"',
        "dataset": '"synthetic"',
        "model": '"local-deterministic"',
        "architecture": '"single-agent"',
        "retrieval": '"lexical"',
        "repetitions": "1",
        "max_tokens": "64",
        "max_minutes": "1",
        "retry_budgets": "[0]",
        "timeout_seconds": "10",
    }
    fields.update({key: str(value) for key, value in extra.items()})
    path = tmp_path / "manifest.toml"
    path.write_text("\n".join(f"{key} = {value}" for key, value in fields.items()) + "\n")
    return path


def synthetic_tasks(tmp_path: Path, count: int) -> tuple[Path, list[str]]:
    repository = tmp_path / "fixture"
    commit = make_repo(repository)
    lines = []
    for index in range(count):
        lines.append(
            json.dumps(
                {
                    "schema_version": 2,
                    "task_id": f"synthetic-{index}",
                    "issue": {"repository": "local/fixture", "title": "Return two", "body": ""},
                    "workspace": str(repository),
                    "base_commit": commit,
                    "test_commands": [EXPECT_TWO],
                    "verification_tests": [EXPECT_TWO],
                    "timeout_seconds": 10,
                }
            )
        )
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text("\n".join(lines) + "\n")
    return dataset, [f"synthetic-{index}" for index in range(count)]


def test_trial_orders_are_reproducible_and_counterbalanced(tmp_path) -> None:
    dataset, _ = synthetic_tasks(tmp_path, 3)
    tasks = BenchmarkTask.load_jsonl(dataset)
    randomized = ExperimentManifest.load(
        manifest_file(tmp_path, trial_order='"randomized"', seed=7, retry_budgets="[0, 1]")
    )
    first = [(t.task_id, b) for t, _, b, _ in plan_trials(tasks, randomized)]
    assert first == [(t.task_id, b) for t, _, b, _ in plan_trials(tasks, randomized)]
    assert sorted(first) == sorted((t.task_id, b) for t in tasks for b in (0, 1))

    balanced = ExperimentManifest.load(
        manifest_file(tmp_path, trial_order='"counterbalanced"', retry_budgets="[0, 1]")
    )
    order = [(t.task_id, b) for t, _, b, _ in plan_trials(tasks, balanced)]
    firsts = [order[index][1] for index in range(0, len(order), 2)]
    assert firsts == [0, 1, 0]  # the first configuration rotates per task


def test_live_providers_need_an_explicit_budget_and_authorization(tmp_path) -> None:
    unbounded = ExperimentManifest.load(manifest_file(tmp_path, model='"groq/model"'))
    with pytest.raises(LiveProviderNotAuthorized, match="max_provider_calls"):
        authorize_live_run(unbounded, True)
    bounded = ExperimentManifest.load(
        manifest_file(tmp_path, model='"groq/model"', max_provider_calls=10)
    )
    with pytest.raises(LiveProviderNotAuthorized, match="authorize-live-provider"):
        authorize_live_run(bounded, False)
    authorize_live_run(bounded, True)
    authorize_live_run(ExperimentManifest.load(manifest_file(tmp_path)), False)


def test_quota_aware_stopping_excludes_provider_failures(tmp_path, monkeypatch) -> None:
    dataset, _ = synthetic_tasks(tmp_path, 5)
    manifest = manifest_file(tmp_path, max_consecutive_provider_failures=2)

    class QuotaExhausted:
        async def generate(self, messages, tools=None):
            raise RuntimeError("Groq API rejected the request (401): quota exhausted")

    monkeypatch.setattr("tender.runner.provider_for", lambda config: QuotaExhausted())
    output = run_experiment_sync(manifest, dataset, tmp_path / "out" / "result.json")

    assert output["stop_reason"] == "consecutive_provider_failures"
    assert output["planned_trials"] == 5 and output["executed_trials"] == 2
    assert output["status"] == "partial"
    assert all(record["exclusion_reason"] == "provider_failure" for record in output["runs"])
    summary = output["summary"]
    assert summary["included_runs"] == 0 and summary["solve_rate_included"] is None
    assert summary["excluded_runs"] == {"provider_failure": 2}


def test_uncertainty_and_matched_comparison_helpers() -> None:
    assert wilson_interval(8, 10) == (0.4902, 0.9433)
    assert wilson_interval(0, 0) is None
    assert sign_test_p_value(5, 0) == 0.0625
    assert sign_test_p_value(0, 0) is None


def test_matched_task_comparison_ignores_excluded_trials(tmp_path) -> None:
    def record(run_id, task, limit, passed, excluded=False):
        return {
            "run_id": run_id,
            "task_id": task,
            "model": "m",
            "architecture": "a",
            "retrieval": "lexical",
            "retrieval_limit": limit,
            "passed": passed,
            "attempts": 1,
            "retry_budget": 0,
            "latency_ms": 1,
            "cost_usd": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "failure_category": "provider_error" if excluded else ("none" if passed else "test"),
            "excluded_from_solve_rate": excluded,
            "exclusion_reason": "provider_failure" if excluded else None,
        }

    payload = {
        "complete": True,
        "status": "complete",
        "manifest": {"retry_budgets": [0], "repetitions": 1},
        "dataset": {"sha256": "same"},
    }
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(
        json.dumps({**payload, "runs": [record("1", "t1", 12, False), record("2", "t2", 12, True)]})
    )
    second.write_text(
        json.dumps(
            {**payload, "runs": [record("3", "t1", 6, True), record("4", "t2", 6, False, True)]}
        )
    )

    comparison = compare_experiment_results([first, second])["matched_tasks"]

    assert comparison["matched_tasks"] == 1
    assert comparison["unmatched_tasks"] == ["t2"]
    assert comparison["baseline"]["retrieval_limit"] == 6
