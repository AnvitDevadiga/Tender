"""Replay reproducibility and provenance: the public replay rebuilds from committed inputs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "demo" / "replay" / "records.json"


def test_replay_is_up_to_date_and_deterministic() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/record_replay.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]


def test_replay_rebuilds_in_a_fresh_tree_without_local_artifacts(tmp_path) -> None:
    """Only committed inputs are copied: no results/, .tender-data/, or databases."""
    for relative in ("src", "scripts", "demo/data", "dashboard", "assets", "pyproject.toml"):
        source = ROOT / relative
        target = tmp_path / relative
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__", "*.db", "records.json")
            )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    assert not (tmp_path / "results").exists() and not (tmp_path / ".tender-data").exists()

    completed = subprocess.run(
        [sys.executable, "-I", "scripts/record_replay.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    rebuilt = (tmp_path / "demo" / "replay" / "records.json").read_bytes()
    assert rebuilt == RECORDS.read_bytes()


def test_replay_labels_recorded_derived_and_unknown_values() -> None:
    snapshot = json.loads(RECORDS.read_text(encoding="utf-8"))
    assert "generated_at" not in snapshot  # no wall-clock timestamps in a deterministic build
    assert set(snapshot["provenance_legend"]) == {"recorded", "derived", "unknown", "verified"}
    for run in snapshot["runs"]:
        provenance = run["provenance"]
        assert provenance["record"] == "recorded" and provenance["events"] == "recorded"
        assert provenance["fields"]["outcome"].startswith("unknown")
        assert provenance["fields"]["regression"].startswith("unknown")
    for events in snapshot["events"].values():
        assert events and all(event["provenance"] == "recorded" for event in events)
    assert all(entry["provenance"] == "unknown" for entry in snapshot["artifacts"].values())
    assert snapshot["metrics"]["provenance"].startswith("derived")


def test_follow_up_run_uses_its_recorded_values_not_copied_ones() -> None:
    """Erratum E8: the v1 replay substituted zero tool calls, a midnight time, and another
    run's configuration for the follow-up run. The recorded values are used now."""
    snapshot = json.loads(RECORDS.read_text(encoding="utf-8"))
    follow_up = next(run for run in snapshot["runs"] if run["id"].startswith("2a5ce4e0"))
    assert follow_up["tool_calls"] == 14
    assert follow_up["config"]["sandbox"] == "docker"
    assert follow_up["created_at"] == "2026-10-09T15:57:11.986147Z"
    names = [event["name"] for event in snapshot["events"][follow_up["id"]]]
    assert names[0] == "run.queued" and "worker.claimed" in names
