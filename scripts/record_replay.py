#!/usr/bin/env python3
"""Regenerate the static replay snapshot served with the portfolio demo.

Inputs are committed to the repository, so a fresh clone can rebuild the replay
byte-for-byte without any local databases or ignored artifacts:

* demo/data/replay-inputs/legacy-runs.v1.json – recorded run payloads and
  recorded event streams for the 19 published runs (exported once by
  scripts/export_replay_inputs.py on the recording machine).
* demo/data/patch-demo/*/report.json – reports produced by running the
  deterministic patch-verification demo (scripts/run_patch_demo.py).

Provenance rules:

* Run payloads and events are labelled ``recorded``; they are copied, never
  rebuilt. Earlier versions of this script reconstructed events and invented a
  creation time; see docs/EVALUATION_ERRATA.md (E8).
* Sanitized fields are labelled ``derived`` with the reason.
* Properties a legacy record never captured (separate outcomes, artifact
  files) are labelled ``unknown``; they are not zero-filled.
* Summaries and metrics are computed by Tender's own API and store code from
  the recorded payloads, and are labelled ``derived``.

The output is deterministic: no wall-clock timestamps, sorted keys, and a digest
of every input.

    python scripts/record_replay.py
    python scripts/record_replay.py --check   # fail if records.json is stale
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGACY_INPUT = REPO_ROOT / "demo" / "data" / "replay-inputs" / "legacy-runs.v1.json"
REPORT_DIR = REPO_ROOT / "demo" / "data" / "patch-demo"
OUTPUT_PATH = REPO_ROOT / "demo" / "replay" / "records.json"
SCHEMA = "tender.dashboard.replay.v2"

SECRET_PATTERNS = {
    "local path": re.compile(r"/Users/|/private/var/|/var/folders/"),
    "groq key": re.compile(r"\bgsk_[A-Za-z0-9]{10,}"),
    "openai key": re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    "github token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{10,}"),
    "bearer header": re.compile(r"(?i)authorization:\s*bearer"),
}
LEGEND = {
    "recorded": "Copied from what Tender recorded when the run executed.",
    "derived": "Computed by Tender code from recorded values, or sanitized for publication.",
    "unknown": "Not captured by the original record; shown as unknown, never as zero.",
    "verified": "Re-checked by an independent clean-workspace verification that was recorded.",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def legacy_runs() -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    document = json.loads(LEGACY_INPUT.read_text(encoding="utf-8"))
    sanitized: dict[str, list[str]] = {}
    for item in document["transformations"]:
        sanitized.setdefault(item["run_id"], []).append(item["field"])
    runs, events = [], {}
    for item in document["runs"]:
        run = dict(item["run"])
        run_id = run["id"]
        published = item.get("published_record")
        verification = run.get("metadata", {}).get("verification_results") or any(
            event["name"] == "task_verification.completed" and event["payload"].get("passed")
            for event in item["events"]
        )
        fields = {field: "derived (sanitized local path)" for field in sanitized.get(run_id, [])}
        fields["outcome"] = "unknown (legacy record predates separate outcomes)"
        fields["artifacts"] = "unknown (artifact directories were not published)"
        fields["regression"] = "unknown (no separate regression suite; see erratum E6)"
        if item["source"] == "forgecalc-001-live-verified-followup":
            fields["config.retrieval"] = "recorded (label 'text' at recording time)"
        else:
            fields["config.retrieval"] = (
                "recorded label 'bm25'; the retriever is lexical, not BM25 (erratum E7)"
            )
        run["provenance"] = {
            "record": "recorded",
            "events": "recorded",
            "source": item["source"],
            "input": str(LEGACY_INPUT.relative_to(REPO_ROOT)),
            "legacy": True,
            "independent_verification": "verified" if verification else "unknown",
            "fields": dict(sorted(fields.items())),
            "published_record": published,
        }
        runs.append(run)
        events[run_id] = [{**event, "provenance": "recorded"} for event in item["events"]]
    return runs, events, document["transformations"]


def patch_reports() -> list[dict]:
    if not REPORT_DIR.is_dir():
        return []
    reports = []
    for path in sorted(REPORT_DIR.glob("*/report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        report["provenance"] = {
            "record": "recorded",
            "source": str(path.relative_to(REPO_ROOT)),
            "note": "Produced by running scripts/run_patch_demo.py; copied as written.",
        }
        reports.append(report)
    return reports


def capture(runs: list[dict]) -> dict:
    """Compute list summaries and metrics with Tender's real API and store code."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="tender-replay-") as temp:
        os.environ["TENDER_DB_PATH"] = str(Path(temp) / "replay.db")
        from fastapi.testclient import TestClient

        import tender.api as api
        from tender.models import EvaluationRun
        from tender.store import RunStore

        store = RunStore(":memory:")
        api.service.store = store
        for payload in runs:
            clean = {key: value for key, value in payload.items() if key != "provenance"}
            store.save_run(EvaluationRun.model_validate(clean))
        client = TestClient(api.app)
        listing = client.get("/runs", params={"summary": "true", "limit": 1000})
        listing.raise_for_status()
        metrics = client.get("/metrics").json()
    provenance = {run["id"]: run["provenance"] for run in runs}
    summaries = [{**row, "provenance": provenance[row["id"]]} for row in listing.json()]
    metrics["provenance"] = "derived (computed by RunStore.aggregate_metrics from recorded runs)"
    return {
        "summaries": summaries,
        "total": int(listing.headers["X-Total-Count"]),
        "metrics": metrics,
    }


def sanitize(document: dict) -> None:
    text = json.dumps(document)
    for label, pattern in SECRET_PATTERNS.items():
        found = pattern.search(text)
        if found:
            raise SystemExit(
                f"refusing to write snapshot: found {label} near {found.group(0)[:40]!r}"
            )


def build() -> str:
    runs, events, transformations = legacy_runs()
    reports = patch_reports()
    captured = capture(runs)
    if captured["metrics"]["runs"] != len(runs):
        raise SystemExit("snapshot metrics do not cover every recorded run")
    inputs = [{"path": str(LEGACY_INPUT.relative_to(REPO_ROOT)), "sha256": digest(LEGACY_INPUT)}]
    inputs += (
        [
            {"path": str(path.relative_to(REPO_ROOT)), "sha256": digest(path)}
            for path in sorted(REPORT_DIR.glob("*/report.json"))
        ]
        if REPORT_DIR.is_dir()
        else []
    )
    document = {
        "schema": SCHEMA,
        "generated_by": "scripts/record_replay.py",
        "deterministic": True,
        "inputs": inputs,
        "provenance_legend": LEGEND,
        "sanitization": transformations,
        "runs": runs,
        "summaries": captured["summaries"],
        "total": captured["total"],
        "metrics": captured["metrics"],
        "events": events,
        "artifacts": {
            run["id"]: {
                "available": False,
                "files": [],
                "provenance": "unknown",
                "note": "Artifact directories were not published; payloads and events were.",
            }
            for run in runs
        },
        "reports": reports,
    }
    sanitize(document)
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when records.json is stale")
    args = parser.parse_args()
    text = build()
    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.is_file() else ""
        if current != text:
            raise SystemExit("demo/replay/records.json is stale; run scripts/record_replay.py")
        print("demo/replay/records.json is up to date")
        return
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    document = json.loads(text)
    metrics = document["metrics"]
    print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)} ({OUTPUT_PATH.stat().st_size:,} bytes)")
    print(
        f"  runs: {len(document['runs'])} | passed: {metrics['passed']} | "
        f"recorded events: {sum(len(items) for items in document['events'].values())} | "
        f"patch reports: {len(document['reports'])}"
    )


if __name__ == "__main__":
    main()
