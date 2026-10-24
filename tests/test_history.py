"""Historical experimental records keep the exact bytes of the audited commit."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDITED_COMMIT = "57144557e12e0731463f96677ac904968e51c78e"
HISTORICAL = (
    "docs/experiments/groq-retry-ablation-3tasks-v1.json",
    "docs/experiments/retrieval-cap-ab-v1.json",
    "docs/experiments/retrieval-cap-ab-v1.md",
    "docs/PROVIDER_TRIALS.md",
    "experiments/groq-retry-ablation-3tasks-v1.toml",
    "experiments/retrieval-cap-ab-v1.toml",
    "experiments/retry-ablation-v1.toml",
    "benchmarks/tasks.jsonl",
    "benchmarks/paired-tasks-v1.jsonl",
    "demo/data/verified-run.json",
)


def _audited_bytes(path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "show", f"{AUDITED_COMMIT}:{path}"], cwd=ROOT, capture_output=True
    )
    return completed.stdout if completed.returncode == 0 else None


@pytest.mark.parametrize("path", HISTORICAL)
def test_historical_record_is_byte_identical_to_the_audited_commit(path) -> None:
    original = _audited_bytes(path)
    if original is None:
        pytest.skip("audited commit is not available in this checkout (shallow clone)")
    assert (ROOT / path).read_bytes() == original
