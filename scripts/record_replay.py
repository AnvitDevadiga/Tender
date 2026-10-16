#!/usr/bin/env python3
"""Record the static dashboard replay snapshot served with the portfolio demo.

The replay page (demo/replay/) loads the real dashboard bundle and answers its
requests from demo/replay/records.json. This script regenerates that snapshot:

1. Loads the published Groq retry-ablation experiment
   (docs/experiments/groq-retry-ablation-3tasks-v1.json) together with the
   recorded EvaluationRun payloads under results/artifacts/<run_id>/run.json.
2. Loads the recorded follow-up verification from demo/data/verified-run.json.
3. Replays every run into a throwaway SQLite store and captures the real API
   responses (summary list, metrics, events, artifact inventory) so the
   snapshot mirrors the live endpoints exactly.
4. Refuses to write output that contains local paths or secret-like strings.

Timestamps: run creation times and latencies are recorded; the finished time is
creation plus latency. Event payloads are recorded values verbatim; the
verification event is placed at finish minus the recorded check durations.

Run from the repository root:

    python scripts/record_replay.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

REPO_ROOT = Path(__file__).resolve().parent.parent
ABLATION_PATH = REPO_ROOT / "docs" / "experiments" / "groq-retry-ablation-3tasks-v1.json"
VERIFIED_PATH = REPO_ROOT / "demo" / "data" / "verified-run.json"
ARTIFACT_ROOT = REPO_ROOT / "results" / "artifacts"
OUTPUT_PATH = REPO_ROOT / "demo" / "replay" / "records.json"
TMP_DB = REPO_ROOT / ".tender-tmp" / "replay-snapshot.db"

BASE_COMMIT_RE = re.compile(r"Base commit: ([0-9a-f]{40})")
SECRET_PATTERNS = {
    "local path": re.compile(r"/Users/"),
    "groq key": re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    "github token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{10,}"),
    "bearer header": re.compile(r"(?i)authorization:\s*bearer"),
}


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_ablation_runs() -> list[dict]:
    """Load the 18 published model trials from their recorded run payloads."""
    ablation = json.loads(ABLATION_PATH.read_text(encoding="utf-8"))
    runs: list[dict] = []
    for record in ablation["runs"]:
        run_id = record["run_id"]
        payload_path = ARTIFACT_ROOT / run_id / "run.json"
        if not payload_path.is_file():
            raise SystemExit(f"missing recorded run payload: {payload_path}")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        recorded_passed = payload["status"] == "passed"
        if recorded_passed is not bool(record["passed"]):
            raise SystemExit(f"recorded status disagrees with the published record: {run_id}")
        metadata = payload.setdefault("metadata", {})
        metadata["replay_source"] = "docs/experiments/groq-retry-ablation-3tasks-v1.json"
        metadata["task_id"] = record["task_id"]
        metadata["retry_budget"] = record["retry_budget"]
        metadata["repetition"] = record["repetition"]
        estimate = record.get("published_rate_cost_estimate_usd")
        if estimate is not None:
            metadata["published_rate_cost_estimate_usd"] = estimate
        runs.append(payload)
    return runs


def load_verified_run(reference_config: dict) -> dict:
    """Rebuild the follow-up verification from its recorded demo form."""
    demo = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))
    evidence = " ".join(step.get("evidence", "") for step in demo.get("steps", []))
    match = BASE_COMMIT_RE.search(evidence)
    if match is None:
        raise SystemExit("the demo record no longer carries the base commit")
    base_commit = match.group(1)
    issue_id = uuid5(NAMESPACE_URL, f"tender-replay:{demo['run_id']}")
    return {
        "id": demo["run_id"],
        "issue": {
            "id": str(issue_id),
            "repository": "local/forgecalc",
            "title": demo["task"],
            "body": "",
            "base_ref": "main",
            "base_commit": base_commit,
        },
        "config": dict(reference_config),
        "benchmark_id": demo["benchmark_id"],
        "status": "passed",
        "failure_category": "none",
        "current_state": "complete",
        "attempts": demo["attempts"],
        "tool_calls": 0,
        "input_tokens": demo["input_tokens"],
        "output_tokens": demo["output_tokens"],
        "cost_usd": demo.get("reported_cost_usd"),
        "latency_ms": demo["latency_ms"],
        "patch": demo["patch"],
        "test_output": None,
        "created_at": "2026-10-09T00:00:00Z",
        "metadata": {
            "base_commit": base_commit,
            "recorded_at": demo["recorded_at"],
            "replay_source": "demo/data/verified-run.json",
            "replay_provenance": {
                "not_recorded": ["tool_calls", "test_output", "created_at time of day"],
                "config_from": "groq-retry-ablation-3tasks-v1 fixture configuration",
            },
        },
    }


def build_events(payload: dict) -> list[dict]:
    """Derive the timeline from recorded fields only (see module docstring)."""
    created = parse_timestamp(payload["created_at"])
    latency_ms = payload.get("latency_ms") or 0
    finished = created + timedelta(milliseconds=latency_ms)
    config = payload["config"]
    events = [
        (
            created,
            "run.queued",
            {"benchmark_id": payload["benchmark_id"]},
        ),
        (
            created,
            "workflow.started",
            {
                "model": config["model"],
                "architecture": config["architecture"],
                "retrieval": config["retrieval"],
                "max_retries": config["max_retries"],
                "sandbox": config["sandbox"],
            },
        ),
    ]
    checks = payload.get("metadata", {}).get("verification_results")
    if checks:
        duration_ms = sum(int(check.get("duration_ms") or 0) for check in checks)
        events.append(
            (
                finished - timedelta(milliseconds=duration_ms),
                "benchmark.verification.completed",
                {
                    "passed": all(check.get("passed") for check in checks),
                    "checks": checks,
                },
            )
        )
    events.append(
        (
            finished,
            "workflow.finished",
            {
                "status": payload["status"],
                "failure_category": payload["failure_category"],
            },
        )
    )
    return [
        {
            "run_id": payload["id"],
            "name": name,
            "timestamp": timestamp.isoformat(),
            "payload": body,
        }
        for timestamp, name, body in events
    ]


def capture(runs: list[dict]) -> dict:
    """Replay runs through the real API into a throwaway store and capture responses."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    os.environ["TENDER_DB_PATH"] = str(TMP_DB)
    TMP_DB.parent.mkdir(parents=True, exist_ok=True)
    for stale in (TMP_DB, Path(f"{TMP_DB}-wal"), Path(f"{TMP_DB}-shm")):
        stale.unlink(missing_ok=True)

    from fastapi.testclient import TestClient

    from tender.api import app
    from tender.models import EvaluationRun, RunEvent
    from tender.store import RunStore

    store = RunStore(TMP_DB)
    events_by_run: dict[str, list[dict]] = {}
    for payload in runs:
        run = EvaluationRun.model_validate(payload)
        store.save_run(run)
        events = build_events(payload)
        events_by_run[str(run.id)] = events
        for event in events:
            store.append_event(RunEvent.model_validate(event))

    client = TestClient(app)
    summary_response = client.get("/runs", params={"summary": "true", "limit": 1000})
    summary_response.raise_for_status()
    metrics = client.get("/metrics").json()
    run_ids = [payload["id"] for payload in runs]
    events = {
        run_id: client.get(f"/runs/{run_id}/events", params={"limit": 20000}).json()
        for run_id in run_ids
    }
    artifacts = {run_id: client.get(f"/runs/{run_id}/artifact").json() for run_id in run_ids}
    return {
        "summaries": summary_response.json(),
        "total": int(summary_response.headers["X-Total-Count"]),
        "metrics": metrics,
        "events": events,
        "artifacts": artifacts,
        "events_by_run": events_by_run,
    }


def sanitize(document: dict) -> None:
    """Fail loudly when the snapshot would leak local paths or secret-like strings."""
    text = json.dumps(document)
    for label, pattern in SECRET_PATTERNS.items():
        found = pattern.search(text)
        if found:
            raise SystemExit(f"refusing to write snapshot: found {label} near {found.group(0)[:40]!r}")


def main() -> None:
    ablation_runs = load_ablation_runs()
    verified = load_verified_run(ablation_runs[0]["config"])
    runs = [verified, *ablation_runs]

    captured = capture(runs)
    if captured["metrics"]["runs"] != len(runs):
        raise SystemExit("snapshot metrics do not cover every recorded run")
    without_patch = [payload["id"] for payload in runs if not payload.get("patch")]
    if without_patch:
        raise SystemExit(f"runs recorded without a patch cannot show the diff view: {without_patch}")

    document = {
        "schema": "tender.dashboard.replay.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "scripts/record_replay.py",
        "reconstruction_notes": (
            "Created and finished times are recorded or derived from recorded latency. "
            "Event payload values are recorded verbatim; the verification event is placed at "
            "finish minus the recorded check durations. The follow-up run's tool_calls, "
            "test_output, and created-at time of day were not part of its recorded form and "
            "are marked under replay_provenance."
        ),
        "sources": [
            "docs/experiments/groq-retry-ablation-3tasks-v1.json (18 recorded model trials)",
            "demo/data/verified-run.json (1 recorded follow-up verification)",
        ],
        "runs": runs,
        "summaries": captured["summaries"],
        "total": captured["total"],
        "metrics": captured["metrics"],
        "events": captured["events"],
        "artifacts": captured["artifacts"],
    }
    sanitize(document)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(document, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for stale in (TMP_DB, Path(f"{TMP_DB}-wal"), Path(f"{TMP_DB}-shm")):
        stale.unlink(missing_ok=True)

    metrics = captured["metrics"]
    available = sum(1 for entry in captured["artifacts"].values() if entry.get("available"))
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({OUTPUT_PATH.stat().st_size:,} bytes)")
    print(f"  runs: {len(runs)} | passed: {metrics['passed']} | artifacts available: {available}")
    print(
        f"  mean latency: {metrics['average_latency_ms']:.0f} ms"
        f" | tokens: {metrics['total_input_tokens']:,} in / {metrics['total_output_tokens']:,} out"
        f" | provider-reported cost observations: {metrics['cost_observations']}"
    )


if __name__ == "__main__":
    main()
