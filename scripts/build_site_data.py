#!/usr/bin/env python3
"""Build the small, derived data file the public landing page renders.

Every figure on the landing page comes from demo/data/site-summary.json, and
every entry in that file names its source file and the JSON path it was read
from, so a reader can check each number. Inputs are committed files only:

* docs/experiments/*.json            published experiment records (recorded)
* demo/data/replay-inputs/*.json     recorded run payloads and events
* demo/data/patch-demo/*/report.json  patch verification demo reports

Nothing is estimated here except where a source already labels a value as an
estimate. Experiments are never merged: each dataset is reported separately,
and legacy records whose outcomes were not separated are labelled as such.

    python scripts/build_site_data.py           # write
    python scripts/build_site_data.py --check   # fail if the file is stale
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "demo" / "data" / "site-summary.json"
ABLATION = "docs/experiments/groq-retry-ablation-3tasks-v1.json"
RETRIEVAL = "docs/experiments/retrieval-cap-ab-v1.json"
REPLAY_INPUTS = "demo/data/replay-inputs/legacy-runs.v1.json"
DEMO_INDEX = "demo/data/patch-demo/index.json"
FOLLOW_UP = "2a5ce4e0-ca02-4f85-9b7e-e541035b51e5"

#: Outcome categories, in display order.
CATEGORIES = [
    ("verified_pass", "Verified pass"),
    ("target_failure", "Target failure"),
    ("regression_failure", "Regression failure"),
    ("test_failure_unseparated", "Test failure, unseparated"),
    ("provider_error", "Provider error"),
    ("environment_error", "Environment error"),
    ("timeout", "Timeout"),
    ("not_evaluated", "Not evaluated"),
]


def load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def legacy_outcomes(records: list[dict]) -> dict[str, int]:
    """Map legacy experiment records onto the outcome categories without guessing."""
    counts = dict.fromkeys((key for key, _ in CATEGORIES), 0)
    for record in records:
        category = record.get("failure_category")
        if record.get("passed"):
            counts["verified_pass"] += 1
        elif category == "provider_error":
            counts["provider_error"] += 1
        elif category in {"environment_setup", "tooling"}:
            counts["environment_error"] += 1
        elif category == "timeout":
            counts["timeout"] += 1
        elif category == "test":
            counts["test_failure_unseparated"] += 1
        else:  # planning, patch, retrieval: the verifier never ran
            counts["not_evaluated"] += 1
    return counts


def report_outcome(report: dict) -> str:
    outcome = report["outcome"]
    if outcome["overall"] == "accepted":
        return "verified_pass"
    if outcome["timed_out"]:
        return "timeout"
    if outcome["environment"] != "ok":
        return "environment_error"
    if outcome["target"] == "failed":
        return "target_failure"
    if outcome["regression"] == "failed":
        return "regression_failure"
    return "not_evaluated"


def build() -> dict:
    ablation = load(ABLATION)
    retrieval = load(RETRIEVAL)
    inputs = load(REPLAY_INPUTS)
    demo = load(DEMO_INDEX)
    reports = [(entry, load(f"demo/data/patch-demo/{entry['file']}")) for entry in demo["reports"]]
    follow_up = next(item for item in inputs["runs"] if item["run"]["id"] == FOLLOW_UP)

    demo_counts = dict.fromkeys((key for key, _ in CATEGORIES), 0)
    for _, report in reports:
        demo_counts[report_outcome(report)] += 1
    outcomes = [
        {
            "id": "patch-demo",
            "label": "Patch verification demo",
            "detail": f"{len(reports)} candidate patches, one bug, no model",
            "trials": len(reports),
            "counts": demo_counts,
            "provenance": "verified",
            "note": "Current outcome model: target and regression checks recorded separately.",
            "source": DEMO_INDEX,
            "pointer": "/reports/*/file → report.json /outcome",
        },
        {
            "id": "retry-ablation",
            "label": "Retry ablation",
            "detail": "{runs} trials, {unique_tasks} tasks".format(**ablation["summary"]),
            "trials": len(ablation["runs"]),
            "counts": legacy_outcomes(ablation["runs"]),
            "provenance": "derived",
            "note": (
                "Legacy record: a pass means the independent target check passed on a clean "
                "copy; regression preservation was not measured (erratum E6)."
            ),
            "source": ABLATION,
            "pointer": "/runs/*/passed, /runs/*/failure_category",
        },
        {
            "id": "retrieval-cap",
            "label": "Retrieval cap A/B",
            "detail": "{runs} trials, {unique_tasks} tasks".format(**retrieval["summary"]),
            "trials": len(retrieval["runs"]),
            "counts": legacy_outcomes(retrieval["runs"]),
            "provenance": "derived",
            "note": (
                "Legacy record. Test failures were not split into target and regression; "
                "one planning failure stopped before verification."
            ),
            "source": RETRIEVAL,
            "pointer": "/runs/*/passed, /runs/*/failure_category",
        },
    ]

    tasks: dict[str, list[dict]] = {}
    for record in ablation["runs"]:
        tasks.setdefault(record["task_id"], []).append(
            {
                "run_id": record["run_id"],
                "seconds": round(record["latency_ms"] / 1000, 2),
                "retry_budget": record["retry_budget"],
                "attempts": record["attempts"],
                "tokens": record["input_tokens"] + record["output_tokens"],
                "estimated_cost_usd": record.get("published_rate_cost_estimate_usd"),
            }
        )
    durations = {
        "label": "Recorded run duration per task",
        "experiment": ablation["manifest"]["name"],
        "model": ablation["manifest"]["model"],
        "provenance": "recorded",
        "source": ABLATION,
        "pointer": "/runs/*/latency_ms",
        "note": (
            "Same model, workflow, and fixture repository for all trials. Every trial passed "
            "on its first attempt, so retry budgets did not change behaviour."
        ),
        "tasks": [
            {
                "task_id": task_id,
                "title": next(
                    item["run"]["issue"]["title"]
                    for item in inputs["runs"]
                    if item["run"]["id"] == runs[0]["run_id"]
                ),
                "median_seconds": round(median(run["seconds"] for run in runs), 2),
                "runs": sorted(runs, key=lambda run: run["seconds"]),
            }
            for task_id, runs in sorted(tasks.items())
        ],
    }

    by_task: dict[str, dict[int, dict]] = {}
    for record in retrieval["runs"]:
        by_task.setdefault(record["task_id"], {})[record["retrieval_limit"]] = record
    pairs = [
        {
            "task_id": task_id,
            "cap_12": {
                "seconds": round(runs[12]["latency_ms"] / 1000, 1),
                "passed": runs[12]["passed"],
                "run_id": runs[12]["run_id"],
            },
            "cap_6": {
                "seconds": round(runs[6]["latency_ms"] / 1000, 1),
                "passed": runs[6]["passed"],
                "run_id": runs[6]["run_id"],
            },
        }
        for task_id, runs in sorted(by_task.items())
        if len(runs) == 2 and all(r["failure_category"] != "provider_error" for r in runs.values())
    ]
    paired = {
        "label": "Matched pairs without provider errors",
        "experiment": retrieval["manifest"]["name"],
        "model": retrieval["manifest"]["model"],
        "provenance": "recorded",
        "source": RETRIEVAL,
        "pointer": "/runs/*/latency_ms where both caps lack provider_error",
        "planned_pairs": len(by_task),
        "pairs": pairs,
        "note": (
            f"Only {len(pairs)} of {len(by_task)} planned pairs completed without provider "
            "errors. This is too few to support any efficiency or capability claim."
        ),
    }

    events = follow_up["events"]
    start = datetime.fromisoformat(events[0]["timestamp"])
    states = [event for event in events if event["name"] == "workflow.state_changed"]
    timeline = {
        "run_id": FOLLOW_UP,
        "title": follow_up["run"]["issue"]["title"],
        "model": follow_up["run"]["config"]["model"],
        "sandbox": follow_up["run"]["config"]["sandbox"],
        "provenance": "recorded",
        "note": "State-change timestamps are recorded; stage durations are derived from them.",
        "source": REPLAY_INPUTS,
        "pointer": f"/runs/[id={FOLLOW_UP}]/events[name=workflow.state_changed]",
        "total_seconds": round(
            (datetime.fromisoformat(events[-1]["timestamp"]) - start).total_seconds(), 3
        ),
        "stages": [
            {
                "state": state["payload"]["to"],
                "start_seconds": round(
                    (datetime.fromisoformat(state["timestamp"]) - start).total_seconds(), 3
                ),
                "end_seconds": round(
                    (
                        datetime.fromisoformat(states[index + 1]["timestamp"])
                        if index + 1 < len(states)
                        else datetime.fromisoformat(events[-1]["timestamp"])
                    ).timestamp()
                    - start.timestamp(),
                    3,
                ),
            }
            for index, state in enumerate(states)
        ],
    }

    def model_row(experiment: dict, source: str) -> dict:
        records = experiment["runs"]
        model = experiment["manifest"]["model"]
        provider, _, model_id = model.partition("/")
        measured = [r for r in records if r.get("failure_category") != "provider_error"]
        estimates = [
            r["published_rate_cost_estimate_usd"]
            for r in records
            if r.get("published_rate_cost_estimate_usd") is not None
        ]
        return {
            "model": model_id,
            "model_publisher": model_id.split("/")[0] if "/" in model_id else None,
            "provider": provider,
            "experiment": experiment["manifest"]["name"],
            "unique_tasks": len({r["task_id"] for r in records}),
            "trials": len(records),
            "passed": sum(bool(r["passed"]) for r in records),
            "provider_failures": sum(
                r.get("failure_category") == "provider_error" for r in records
            ),
            "median_seconds_excluding_provider_failures": (
                round(median(r["latency_ms"] for r in measured) / 1000, 1) if measured else None
            ),
            "estimated_cost_usd": round(sum(estimates), 6) if estimates else None,
            "cost_basis": (experiment["manifest"].get("pricing", {}) or {}).get("basis")
            or "unknown",
            "provenance": "derived",
            "source": source,
        }

    models = {
        "note": (
            "One model has been evaluated in published Tender experiments. Rows are per "
            "experiment because the experiments used different tasks and are not comparable."
        ),
        "rows": [model_row(ablation, ABLATION), model_row(retrieval, RETRIEVAL)],
    }

    entry, preview = next((e, r) for e, r in reports if r["outcome"]["overall"] == "accepted")
    target = preview["target"]["checks"][0]
    regression = preview["regression"]["checks"][0]
    hero = {
        "source": f"demo/data/patch-demo/{entry['file']}",
        "provenance": "verified",
        "report_id": preview["report_id"],
        "task": preview["task"]["title"],
        "repository": preview["repository"]["name"],
        "base_commit": preview["repository"]["base_commit"],
        "patch_file": preview["patch"]["file"],
        "patch_sha256": preview["patch"]["sha256"],
        "sandbox": preview["environment"]["sandbox"]["backend"],
        "overall": preview["outcome"]["overall"],
        "independently_verified": preview["outcome"]["independently_verified"],
        "coverage_provenance": target["coverage"]["provenance"],
        "tests": [
            {"group": "target", "id": node, "outcome": "passed" if target["passed"] else "failed"}
            for node in target["coverage"]["executed"]
        ]
        + [
            {
                "group": "regression",
                "id": node,
                "outcome": "passed" if regression["passed"] else "failed",
            }
            for node in regression["coverage"]["executed"]
        ],
        "evidence_manifest_sha256": preview["evidence"]["manifest_sha256"],
    }
    return {
        "schema": "tender.site-summary.v1",
        "generated_by": "scripts/build_site_data.py",
        "categories": [{"key": key, "label": label} for key, label in CATEGORIES],
        "hero_report": hero,
        "outcomes": outcomes,
        "durations": durations,
        "paired_latency": paired,
        "timeline": timeline,
        "models": models,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = json.dumps(build(), indent=1, ensure_ascii=False) + "\n"
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != text:
            raise SystemExit("demo/data/site-summary.json is stale; run scripts/build_site_data.py")
        print("demo/data/site-summary.json is up to date")
        return
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
