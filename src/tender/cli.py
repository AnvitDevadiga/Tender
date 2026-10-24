from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .evidence import verify_bundle
from .experiments import BenchmarkTask, ExperimentManifest
from .patch_eval import PatchEvaluationError, evaluate_patch, render_markdown
from .providers import GroqProvider
from .runner import LiveProviderNotAuthorized, compare_experiment_results, run_experiment_sync
from .worker import serve as serve_worker

EXIT_ACCEPTED, EXIT_REJECTED, EXIT_NOT_EVALUATED = 0, 1, 2


async def _check_groq() -> int:
    if not os.getenv("GROQ_API_KEY"):
        print("GROQ_API_KEY is not configured in this terminal.")
        return 2
    try:
        response = await GroqProvider().generate(
            [{"role": "user", "content": "Reply with exactly: Tender ready"}]
        )
    except Exception as exc:  # provider errors should be visible without exposing credentials
        print(f"Groq request failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"Groq connected. Model response received ({response.output_tokens} output tokens).")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tender", description="Independent, evidence-backed verification for code changes."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser(
        "verify-patch", help="Evaluate a patch against a pinned commit with fixed criteria."
    )
    verify.add_argument("--repo", required=True, help="Local Git repository (read-only).")
    verify.add_argument("--base", required=True, help="Base commit or ref to verify against.")
    verify.add_argument("--patch", required=True, help="Unified Git diff to evaluate.")
    verify.add_argument("--criteria", required=True, help="Acceptance criteria (.toml or .json).")
    verify.add_argument("--out", required=True, help="New directory for report and evidence.")
    verify.add_argument("--sandbox", choices=["docker", "local"], default="docker")
    verify.add_argument(
        "--trust-local-execution",
        action="store_true",
        help="Allow --sandbox local: the patch runs on this machine without isolation.",
    )
    verify.add_argument("--python", help="Python command used for pytest checks.")
    verify.add_argument("--name", help="Repository display name for the report.")

    report = commands.add_parser("report", help="Work with patch verification reports.")
    report_actions = report.add_subparsers(dest="action", required=True)
    render = report_actions.add_parser("render", help="Print a report as Markdown or JSON.")
    render.add_argument("report")
    render.add_argument("--format", choices=["md", "json"], default="md")

    evidence = commands.add_parser("evidence", help="Work with evidence bundles.")
    evidence_actions = evidence.add_subparsers(dest="action", required=True)
    check = evidence_actions.add_parser("verify", help="Recompute every hash in a bundle.")
    check.add_argument("bundle")

    commands.add_parser("check-groq", help="Make one small request to Groq.")
    commands.add_parser("worker", help="Run the queue worker (needs TENDER_WORKSPACE_ROOT).")

    for name in ("run-experiment", "validate-benchmark", "compare"):
        sub = commands.add_parser(name)
        sub.add_argument("--manifest")
        sub.add_argument("--dataset")
        sub.add_argument("--output", default="results.json")
        sub.add_argument("--results", nargs="+")
        sub.add_argument(
            "--authorize-live-provider",
            action="store_true",
            help="Allow run-experiment to call a billable provider within the manifest budget.",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "verify-patch":
        try:
            report = evaluate_patch(
                args.repo,
                args.base,
                args.patch,
                args.criteria,
                args.out,
                sandbox=args.sandbox,
                trust_local_execution=args.trust_local_execution,
                python=args.python,
                repository_name=args.name,
            )
        except (PatchEvaluationError, OSError, ValueError) as exc:
            parser.exit(EXIT_NOT_EVALUATED, f"tender verify-patch: {exc}\n")
        overall = report["outcome"]["overall"]
        print(f"{overall.upper()}: {report['summary']}")
        for line in report.get("explanations", []):
            print(f"  - {line}")
        print(f"report: {Path(args.out) / 'report.json'}")
        if overall == "accepted":
            return EXIT_ACCEPTED
        return EXIT_REJECTED if overall == "rejected" else EXIT_NOT_EVALUATED
    if args.command == "report":
        payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(payload.get("markdown") or render_markdown(payload))
        return 0
    if args.command == "evidence":
        problems = verify_bundle(args.bundle)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}")
            return 1
        print(f"OK every file in {args.bundle} matches its manifest hash (bundle is not signed)")
        return 0
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
        try:
            result = run_experiment_sync(
                args.manifest,
                args.dataset,
                args.output,
                authorize_live_provider=args.authorize_live_provider,
            )
        except LiveProviderNotAuthorized as exc:
            parser.error(str(exc))
        print(f"Experiment {result['status']}: {result['summary']}")
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
