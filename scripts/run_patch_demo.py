#!/usr/bin/env python3
"""Run the deterministic failure-and-repair patch verification demo.

1. Build the fixture repository from examples/patch-demo/repo with a fixed
   author and date, so its base commit ID is identical on every machine.
2. Evaluate candidate-1.patch, then candidate-2.patch, with the same criteria
   (examples/patch-demo/criteria.toml) against the same pinned commit.
3. Write each report, its Markdown summary, and its evidence bundle to
   demo/data/patch-demo/candidate-N/ and an index for the landing page.

No language model is involved and nothing is fabricated: the reports are the
outputs of these evaluations. Durations and timestamps differ between runs.

Sandbox: Docker when the daemon and the tender-sandbox:local image are
available (build it with `docker build -f Dockerfile.sandbox -t
tender-sandbox:local .`); otherwise the local backend, which is acceptable here
only because both candidate patches are part of this repository. The report
records which backend ran.

    python scripts/run_patch_demo.py [--sandbox auto|docker|local]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "patch-demo"
OUTPUT = ROOT / "demo" / "data" / "patch-demo"
IMAGE = "tender-sandbox:local"
FIXED_ENV = {
    "GIT_AUTHOR_NAME": "Tender Demo Fixture",
    "GIT_AUTHOR_EMAIL": "fixtures@example.invalid",
    "GIT_AUTHOR_DATE": "@1760000000 +0000",
    "GIT_COMMITTER_NAME": "Tender Demo Fixture",
    "GIT_COMMITTER_EMAIL": "fixtures@example.invalid",
    "GIT_COMMITTER_DATE": "@1760000000 +0000",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
CANDIDATES = (
    ("candidate-1", "Candidate 1: plausible fix"),
    ("candidate-2", "Candidate 2: corrected fix"),
)

sys.path.insert(0, str(ROOT / "src"))


def build_fixture(destination: Path) -> str:
    shutil.copytree(EXAMPLE / "repo", destination)
    env = {**os.environ, **FIXED_ENV}

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=destination, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "--initial-branch", "main")
    git("add", "--all")
    git("commit", "-q", "-m", "stats-demo fixture baseline")
    return git("rev-parse", "HEAD")


def choose_sandbox(requested: str) -> str:
    from tender.patch_eval import docker_available

    if requested != "auto":
        return requested
    if docker_available():
        image = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
        if image.returncode == 0:
            return "docker"
        print(f"Docker is running but {IMAGE} is missing; using the local backend.")
    else:
        print("Docker is not available; using the local backend for repository-authored patches.")
    return "local"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox", choices=["auto", "docker", "local"], default="auto")
    args = parser.parse_args()
    from tender.patch_eval import evaluate_patch

    sandbox = choose_sandbox(args.sandbox)
    if sandbox == "docker":
        os.environ.setdefault("TENDER_SANDBOX_IMAGE", IMAGE)
    with tempfile.TemporaryDirectory(prefix="tender-demo-") as temp:
        repo = Path(temp) / "stats-demo"
        commit = build_fixture(repo)
        print(f"fixture stats-demo @ {commit} ({sandbox} sandbox)")
        if OUTPUT.exists():
            shutil.rmtree(OUTPUT)
        OUTPUT.mkdir(parents=True)
        index = {
            "scenario": "One bug, two candidate patches, identical criteria and base commit.",
            "base_commit": commit,
            "sandbox": sandbox,
            "generated_by": "scripts/run_patch_demo.py",
            "reports": [],
        }
        for name, label in CANDIDATES:
            report = evaluate_patch(
                repo,
                commit,
                EXAMPLE / f"{name}.patch",
                EXAMPLE / "criteria.toml",
                OUTPUT / name,
                sandbox=sandbox,
                trust_local_execution=sandbox == "local",
                python="python3" if sandbox == "docker" else sys.executable,
                repository_name="stats-demo",
            )
            index["reports"].append(
                {"file": f"{name}/report.json", "label": label, "report_id": report["report_id"]}
            )
            print(f"  {name}: {report['outcome']['overall']} — {report['summary']}")
        (OUTPUT / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}; regenerate the replay with scripts/record_replay.py")


if __name__ == "__main__":
    main()
