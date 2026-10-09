from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .experiments import BenchmarkTask, ExperimentManifest
from .providers import GroqProvider
from .runner import compare_experiment_results, run_experiment_sync
from .worker import serve as serve_worker


async def _check_groq() -> int:
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is not configured in this terminal.")
        return 2
    try:
        response = await GroqProvider().generate(
            [{"role": "user", "content": "Reply with exactly: ForgeBench ready"}]
        )
    except Exception as exc:  # provider errors should be visible without exposing credentials
        print(f"Groq request failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"Groq connected. Model response received ({response.output_tokens} output tokens).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="forgebench")
    parser.add_argument(
        "command",
        choices=["check-groq", "run-experiment", "validate-benchmark", "compare", "worker"],
    )
    parser.add_argument("--manifest")
    parser.add_argument("--dataset")
    parser.add_argument("--output", default="results.json")
    parser.add_argument("--results", nargs="+")
    args = parser.parse_args()
    if args.command == "check-groq":
        return asyncio.run(_check_groq())
    if args.command == "worker":
        asyncio.run(serve_worker())
        return 0
    if args.command == "compare":
        if not args.results:
            parser.error("compare requires at least two --results files")
        try:
            comparison = compare_experiment_results(args.results)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            parser.error(str(exc))
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as file:
            json.dump(comparison, file, indent=2)
        print(json.dumps(comparison, indent=2))
        return 0
    if args.command == "run-experiment":
        if not args.manifest or not args.dataset:
            parser.error("run-experiment requires --manifest and --dataset")
        result = run_experiment_sync(args.manifest, args.dataset, args.output)
        print(f"Experiment complete: {result['summary']}")
        return 0
    if args.command == "validate-benchmark":
        if not args.manifest or not args.dataset:
            parser.error("validate-benchmark requires --manifest and --dataset")
        manifest = ExperimentManifest.load(args.manifest)
        tasks = BenchmarkTask.load_jsonl(args.dataset)
        print(
            json.dumps(
                {
                    "manifest": manifest.name,
                    "tasks": len(tasks),
                    "retry_budgets": manifest.retry_budgets,
                }
            )
        )
        return 0
    return 2
