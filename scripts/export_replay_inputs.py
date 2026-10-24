#!/usr/bin/env python3
"""Export the recorded inputs of the public replay into a committed, sanitized file.

Run once on the machine that recorded the runs. It reads the local run databases
(which are git-ignored) and writes demo/data/replay-inputs/legacy-runs.v1.json,
so that a fresh clone can regenerate the replay with scripts/record_replay.py
without any developer-machine artifacts.

Nothing is reconstructed here: run payloads and events are copied as recorded.
The only edits are sanitization, each listed in the output's "transformations":
container or host artifact paths become "artifacts/<run_id>".

    python scripts/export_replay_inputs.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ABLATION = REPO_ROOT / "docs" / "experiments" / "groq-retry-ablation-3tasks-v1.json"
FOLLOW_UP_ID = "2a5ce4e0-ca02-4f85-9b7e-e541035b51e5"
SOURCES = {
    "ablation": REPO_ROOT / "results" / ".forgebench-runs.db",
    "follow_up": REPO_ROOT / ".tender-data" / "forgebench.db",
}
OUTPUT = REPO_ROOT / "demo" / "data" / "replay-inputs" / "legacy-runs.v1.json"
FORBIDDEN = (
    re.compile(r"/Users/"),
    re.compile(r"/private/"),
    re.compile(r"/var/folders"),
    re.compile(r"\bgsk_[A-Za-z0-9]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{10,}"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{10,}"),
    re.compile(r"(?i)authorization:\s*bearer"),
)


def load(database: Path, run_id: str) -> tuple[dict, list[dict]]:
    connection = sqlite3.connect(database)
    row = connection.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise SystemExit(f"run {run_id} is not recorded in {database}")
    events = [
        {"run_id": run, "name": name, "timestamp": timestamp, "payload": json.loads(payload)}
        for run, name, timestamp, payload in connection.execute(
            "SELECT run_id, name, timestamp, payload FROM run_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]
    connection.close()
    return json.loads(row[0]), events


def sanitize_artifact_path(payload: dict, transformations: list[dict]) -> None:
    metadata = payload.get("metadata", {})
    original = metadata.get("artifact_path")
    if original and original != f"artifacts/{payload['id']}":
        metadata["artifact_path"] = f"artifacts/{payload['id']}"
        transformations.append(
            {
                "run_id": payload["id"],
                "field": "metadata.artifact_path",
                "change": "local or container path replaced with artifacts/<run_id>",
            }
        )


def sanitize_events(events: list[dict], transformations: list[dict]) -> None:
    for event in events:
        path = event["payload"].get("artifact_path")
        if path and path != f"artifacts/{event['run_id']}":
            event["payload"]["artifact_path"] = f"artifacts/{event['run_id']}"
            transformations.append(
                {
                    "run_id": event["run_id"],
                    "field": f"events[{event['name']}].payload.artifact_path",
                    "change": "local or container path replaced with artifacts/<run_id>",
                }
            )


def main() -> None:
    for name, path in SOURCES.items():
        if not path.is_file():
            raise SystemExit(
                f"missing local source {path} ({name}); this export only runs on the recording "
                "machine. A fresh clone uses the committed output instead."
            )
    ablation = json.loads(ABLATION.read_text(encoding="utf-8"))
    transformations: list[dict] = []
    runs = []
    for record in ablation["runs"]:
        payload, events = load(SOURCES["ablation"], record["run_id"])
        for field in ("attempts", "input_tokens", "output_tokens", "latency_ms"):
            if payload[field] != record[field]:
                raise SystemExit(f"{record['run_id']}: {field} disagrees with the published record")
        if (payload["status"] == "passed") is not bool(record["passed"]):
            raise SystemExit(f"{record['run_id']}: status disagrees with the published record")
        sanitize_artifact_path(payload, transformations)
        sanitize_events(events, transformations)
        runs.append(
            {
                "source": "groq-retry-ablation-3tasks-v1",
                "published_record": record,
                "run": payload,
                "events": events,
            }
        )
    payload, events = load(SOURCES["follow_up"], FOLLOW_UP_ID)
    sanitize_artifact_path(payload, transformations)
    sanitize_events(events, transformations)
    runs.insert(
        0,
        {
            "source": "forgecalc-001-live-verified-followup",
            "published_record": None,
            "run": payload,
            "events": events,
        },
    )
    document = {
        "schema": "tender.replay-inputs.v1",
        "description": (
            "Recorded run payloads and event streams for the public replay, exported from the "
            "local run databases that recorded them. Values are copied as recorded; "
            "transformations lists every sanitization edit."
        ),
        "published_experiment": "docs/experiments/groq-retry-ablation-3tasks-v1.json",
        "published_experiment_sha256": hashlib.sha256(ABLATION.read_bytes()).hexdigest(),
        "transformations": transformations,
        "runs": runs,
    }
    text = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    for pattern in FORBIDDEN:
        match = pattern.search(text)
        if match:
            raise SystemExit(f"refusing to write: found {match.group(0)[:40]!r}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text, encoding="utf-8")
    print(
        f"wrote {OUTPUT.relative_to(REPO_ROOT)}: {len(runs)} runs, "
        f"{sum(len(item['events']) for item in runs)} recorded events, "
        f"{len(transformations)} sanitization edits",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
