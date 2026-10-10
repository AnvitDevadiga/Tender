from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from time import monotonic

from .artifacts import ArtifactStore
from .experiments import BenchmarkTask, ExperimentManifest
from .models import BenchmarkConfig, EvaluationRun, FailureCategory, RunEvent, RunStatus
from .runtime import provider_for
from .sandbox import Sandbox
from .services import RunService
from .store import RunStore
from .workflow import PatchApplier


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_final_patch(
    base_workspace: str | Path,
    destination: str | Path,
    patch: str,
    setup_commands: tuple[str, ...],
    commands: tuple[str, ...],
    timeout_seconds: int,
) -> tuple[bool, list[dict[str, object]]]:
    """Run independent checks against a clean copy with the final agent patch applied."""
    shutil.copytree(base_workspace, destination)
    PatchApplier().apply(destination, patch)
    sandbox = Sandbox(destination, timeout_seconds=timeout_seconds)
    results: list[dict[str, object]] = []
    passed = True
    for command in setup_commands + commands:
        checked = sandbox.run(command)
        result = {
            "kind": "setup" if command in setup_commands else "verification",
            "command": command,
            "passed": checked.passed,
            "timed_out": checked.timed_out,
            "return_code": checked.return_code,
            "duration_ms": checked.duration_ms,
            "stdout": checked.stdout[-4000:],
            "stderr": checked.stderr[-4000:],
        }
        results.append(result)
        if not checked.passed:
            passed = False
            if checked.timed_out:
                break
    return passed, results


async def run_experiment(
    manifest_path: str | Path, dataset_path: str | Path, output_path: str | Path
) -> dict[str, object]:
    manifest = ExperimentManifest.load(manifest_path)
    tasks = BenchmarkTask.load_jsonl(dataset_path)
    if not tasks:
        raise ValueError("benchmark dataset contains no tasks")
    if manifest.sandbox != "local":
        raise ValueError(
            "benchmark runner currently supports only the local trusted-fixture sandbox"
        )
    dataset_root = Path.cwd()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results = []
    deadline = monotonic() + manifest.max_minutes * 60
    budget_exhausted = False
    for task in tasks:
        for retrieval_limit in manifest.retrieval_limits:
            for retry_budget in manifest.retry_budgets:
                for repetition in range(manifest.repetitions):
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        budget_exhausted = True
                        break
                    with tempfile.TemporaryDirectory(prefix="tender-") as temp_dir:
                        source_workspace = Path(task.workspace)
                        if not source_workspace.is_absolute() and not source_workspace.exists():
                            source_workspace = dataset_root / source_workspace
                        if not source_workspace.is_dir():
                            raise ValueError(
                                f"benchmark workspace does not exist: {task.workspace}"
                            )
                        source_root = subprocess.run(
                            ["git", "rev-parse", "--show-toplevel"],
                            cwd=source_workspace,
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                        if Path(source_root).resolve() != source_workspace.resolve():
                            raise ValueError(
                                f"task {task.task_id}: workspace must be the Git repository root"
                            )
                        source_status = subprocess.run(
                            ["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=source_workspace,
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout
                        if source_status.strip():
                            raise ValueError(
                                f"task {task.task_id}: source workspace must be clean "
                                "before benchmarking"
                            )
                        workspace = Path(temp_dir) / "repo"
                        shutil.copytree(source_workspace, workspace)
                        subprocess.run(
                            ["git", "checkout", "--detach", task.base_commit],
                            cwd=workspace,
                            check=True,
                            capture_output=True,
                        )
                        actual_commit = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=workspace,
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                        if actual_commit.lower() != task.base_commit.lower():
                            raise ValueError(
                                f"task {task.task_id}: checkout did not resolve to the pinned "
                                "commit"
                            )
                        config = BenchmarkConfig(
                            model=(
                                f"{manifest.model.split('/', 1)[0]}/"
                                f"{manifest.model.split('/', 1)[1]}"
                            )
                            if "/" in manifest.model
                            else manifest.model,
                            architecture=manifest.architecture,
                            retrieval=manifest.retrieval,
                            retrieval_limit=retrieval_limit,
                            max_retries=retry_budget,
                            setup_commands=list(task.setup_commands),
                            commands=list(task.test_commands),
                            static_analysis_commands=list(task.static_analysis_commands),
                            verification_commands=list(task.verification_tests),
                            sandbox=manifest.sandbox,
                            timeout_seconds=min(
                                manifest.timeout_seconds,
                                task.timeout_seconds,
                                int(max(1, remaining)),
                            ),
                            max_output_tokens=manifest.max_tokens,
                        )
                        store = RunStore(output_path.parent / ".tender-runs.db")
                        service = RunService(
                            provider=provider_for(config),
                            store=store,
                            artifacts=ArtifactStore(output_path.parent / "artifacts"),
                        )
                        run = EvaluationRun(
                            issue=task.issue, config=config, benchmark_id=manifest.name
                        )
                        try:
                            run = await asyncio.wait_for(
                                service.execute_workflow(run, str(workspace)),
                                timeout=max(0.1, deadline - monotonic()),
                            )
                        except TimeoutError:
                            run.status = RunStatus.FAILED
                            run.failure_category = FailureCategory.TIMEOUT
                            run.test_output = "Experiment max_minutes budget exhausted."
                            run.metadata["budget_exhausted"] = True
                            service.store.save_run(run)
                            budget_exhausted = True
                        passed = run.status is RunStatus.PASSED
                        verification_results = []
                        target_tests_passed = passed
                        if passed:
                            if not run.patch:
                                passed = False
                                verification_results.append(
                                    {
                                        "passed": False,
                                        "reason": "successful run did not contain a patch artifact",
                                    }
                                )
                            else:
                                try:
                                    verification_workspace = Path(temp_dir) / "verification"
                                    passed, verification_results = verify_final_patch(
                                        workspace,
                                        verification_workspace,
                                        run.patch,
                                        task.setup_commands,
                                        task.verification_tests,
                                        min(
                                            task.timeout_seconds,
                                            int(max(1, deadline - monotonic())),
                                        ),
                                    )
                                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                                    passed = False
                                    verification_results.append(
                                        {
                                            "passed": False,
                                            "reason": (
                                                "final patch verification failed: "
                                                f"{type(exc).__name__}: {str(exc)[:300]}"
                                            ),
                                        }
                                    )
                            run.metadata["verification_results"] = verification_results
                            if not passed:
                                run.status = RunStatus.FAILED
                                run.failure_category = FailureCategory.TEST
                                run.test_output = (
                                    run.test_output or ""
                                ) + "\nIndependent verification failed."
                            service.store.append_event(
                                RunEvent(
                                    run_id=run.id,
                                    name="benchmark.verification.completed",
                                    payload={"passed": passed, "checks": verification_results},
                                )
                            )
                            service.artifacts.record_verification(run, verification_results)
                            service.store.save_run(run)
                        results.append(
                            {
                                "task_id": task.task_id,
                                "task": str(task.issue.id),
                                "repository": task.issue.repository,
                                "base_commit": actual_commit,
                                "retry_budget": retry_budget,
                                "retrieval_limit": retrieval_limit,
                                "repetition": repetition,
                                "run_id": str(run.id),
                                "passed": passed,
                                "target_tests_passed": target_tests_passed,
                                "regression_free": (passed if target_tests_passed else None),
                                "attempts": run.attempts,
                                "failure_category": run.failure_category.value,
                                "latency_ms": run.latency_ms,
                                "patch_bytes": (
                                    len(run.patch.encode("utf-8"))
                                    if run.patch is not None
                                    else None
                                ),
                                "model": config.model,
                                "architecture": config.architecture,
                                "retrieval": config.retrieval,
                                "retrieval_limit": config.retrieval_limit,
                                "input_tokens": run.input_tokens,
                                "output_tokens": run.output_tokens,
                                "cost_usd": run.cost_usd,
                                "published_rate_cost_estimate_usd": _published_rate_cost_estimate(
                                    manifest, run.input_tokens, run.output_tokens
                                ),
                                "tool_calls": run.tool_calls,
                                "verification": verification_results,
                            }
                        )
                    if budget_exhausted:
                        break
                if budget_exhausted:
                    break
        if budget_exhausted:
            break
    output = {
        "schema_version": 2,
        "status": "complete" if not budget_exhausted else "partial",
        "manifest": {
            "name": manifest.name,
            "path": str(manifest_path),
            "sha256": file_sha256(manifest_path),
            "model": manifest.model,
            "architecture": manifest.architecture,
            "retrieval": manifest.retrieval,
            "retrieval_limits": list(manifest.retrieval_limits),
            "repetitions": manifest.repetitions,
            "retry_budgets": list(manifest.retry_budgets),
            "max_minutes": manifest.max_minutes,
            "max_tokens": manifest.max_tokens,
            "sandbox": manifest.sandbox,
            "pricing": {
                "basis": manifest.pricing_basis,
                "input_usd_per_million_tokens": manifest.input_usd_per_million_tokens,
                "output_usd_per_million_tokens": manifest.output_usd_per_million_tokens,
            }
            if manifest.pricing_basis
            else None,
        },
        "dataset": {
            "path": str(dataset_path),
            "sha256": file_sha256(dataset_path),
            "declared_version": manifest.dataset,
        },
        "runs": results,
        "complete": not budget_exhausted
        and len(results)
        == len(tasks)
        * len(manifest.retrieval_limits)
        * len(manifest.retry_budgets)
        * manifest.repetitions,
        "budget_exhausted": budget_exhausted,
    }
    output["summary"] = summarize_from_records(results)
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return output


def summarize_from_records(records: list[dict[str, object]]) -> dict[str, object]:
    count = len(records)
    unique_tasks = len({str(record["task_id"]) for record in records if record.get("task_id")})
    unique_repositories = len(
        {str(record["repository"]) for record in records if record.get("repository")}
    )
    passed = sum(bool(record["passed"]) for record in records)
    provider_failures = sum(
        str(record.get("failure_category")) == FailureCategory.PROVIDER.value
        for record in records
    )
    recovered = sum(bool(record["passed"]) and int(record["attempts"]) > 1 for record in records)
    retryable = sum(int(record["attempts"]) > 1 for record in records)
    verified_target_passes = [
        record for record in records if record.get("target_tests_passed") is True
    ]
    regression_free = sum(
        record.get("regression_free") is True for record in verified_target_passes
    )
    latencies = [
        int(record["latency_ms"])
        for record in records
        if record["latency_ms"] is not None
        and str(record.get("failure_category")) != FailureCategory.PROVIDER.value
    ]
    patch_sizes = [
        int(record["patch_bytes"]) for record in records if record.get("patch_bytes") is not None
    ]
    by_retrieval_limit: dict[str, dict[str, float | int]] = {}
    for limit in sorted({int(record.get("retrieval_limit", 12)) for record in records}):
        subset = [record for record in records if int(record.get("retrieval_limit", 12)) == limit]
        measured = [
            record
            for record in subset
            if str(record.get("failure_category")) != FailureCategory.PROVIDER.value
        ]
        token_measured = [
            record
            for record in measured
            if int(record["input_tokens"]) + int(record["output_tokens"]) > 0
        ]
        by_retrieval_limit[str(limit)] = {
            "runs": len(subset),
            "passed": sum(bool(record["passed"]) for record in subset),
            "pass_rate": sum(bool(record["passed"]) for record in subset) / len(subset)
            if subset
            else 0.0,
            "unique_tasks": len({str(record["task_id"]) for record in subset}),
            "provider_failures": sum(
                str(record.get("failure_category")) == FailureCategory.PROVIDER.value
                for record in subset
            ),
            "token_observations": len(token_measured),
            "average_latency_ms": (
                sum(
                    int(record["latency_ms"])
                    for record in measured
                    if record["latency_ms"] is not None
                )
                / sum(record["latency_ms"] is not None for record in measured)
                if any(record["latency_ms"] is not None for record in measured)
                else None
            ),
            "average_input_tokens": sum(int(record["input_tokens"]) for record in token_measured)
            / len(token_measured)
            if token_measured
            else 0.0,
            "average_output_tokens": sum(int(record["output_tokens"]) for record in token_measured)
            / len(token_measured)
            if token_measured
            else 0.0,
        }
    by_budget: dict[str, dict[str, float | int]] = {}
    for budget in sorted({int(record["retry_budget"]) for record in records}):
        subset = [record for record in records if int(record["retry_budget"]) == budget]
        subset_passed = sum(bool(record["passed"]) for record in subset)
        by_budget[str(budget)] = {
            "runs": len(subset),
            "passed": subset_passed,
            "pass_rate": subset_passed / len(subset) if subset else 0.0,
            "unique_tasks": len(
                {str(record["task_id"]) for record in subset if record.get("task_id")}
            ),
            "average_attempts": sum(int(record["attempts"]) for record in subset) / len(subset)
            if subset
            else 0.0,
            "average_latency_ms": (
                sum(
                    int(record["latency_ms"])
                    for record in subset
                    if record["latency_ms"] is not None
                    and str(record.get("failure_category")) != FailureCategory.PROVIDER.value
                )
                / sum(
                    record["latency_ms"] is not None
                    and str(record.get("failure_category")) != FailureCategory.PROVIDER.value
                    for record in subset
                )
                if any(
                    record["latency_ms"] is not None
                    and str(record.get("failure_category")) != FailureCategory.PROVIDER.value
                    for record in subset
                )
                else None
            ),
        }
    return {
        "runs": count,
        "status": "complete"
        if records and all(record.get("complete", True) for record in records)
        else "partial",
        "unique_tasks": unique_tasks,
        "unique_repositories": unique_repositories,
        "sample_size_warning": (
            "Exploratory only: fewer than 30 unique tasks or 10 repositories; "
            "do not infer model superiority."
            if unique_tasks < 30 or unique_repositories < 10
            else None
        ),
        "provider_failures": provider_failures,
        "solve_rate_excluding_provider_failures": (
            passed / (count - provider_failures) if count > provider_failures else None
        ),
        "passed": passed,
        "pass_rate": passed / count if count else 0.0,
        "average_attempts": sum(int(record["attempts"]) for record in records) / count
        if count
        else 0.0,
        "retry_recovery_rate": recovered / retryable if retryable else 0.0,
        "regression_free_rate": regression_free / len(verified_target_passes)
        if verified_target_passes
        else None,
        "regression_evaluations": len(verified_target_passes),
        "average_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "average_patch_bytes": sum(patch_sizes) / len(patch_sizes) if patch_sizes else None,
        "patch_size_observations": len(patch_sizes),
        "total_cost_usd": (
            sum(float(record["cost_usd"]) for record in records if record["cost_usd"] is not None)
            if any(record["cost_usd"] is not None for record in records)
            else None
        ),
        "total_published_rate_cost_estimate_usd": (
            sum(
                float(record["published_rate_cost_estimate_usd"])
                for record in records
                if record.get("published_rate_cost_estimate_usd") is not None
            )
            if any(record.get("published_rate_cost_estimate_usd") is not None for record in records)
            else None
        ),
        "published_rate_estimate_observations": sum(
            record.get("published_rate_cost_estimate_usd") is not None for record in records
        ),
        "total_tool_calls": sum(int(record.get("tool_calls", 0)) for record in records),
        "total_input_tokens": sum(int(record["input_tokens"]) for record in records),
        "total_output_tokens": sum(int(record["output_tokens"]) for record in records),
        "failure_categories": dict(
            sorted(
                Counter(
                    str(record["failure_category"]) for record in records if not record["passed"]
                ).items()
            )
        ),
        "by_retry_budget": by_budget,
        "by_retrieval_limit": by_retrieval_limit,
    }


def _published_rate_cost_estimate(
    manifest: ExperimentManifest, input_tokens: int, output_tokens: int
) -> float | None:
    if (
        manifest.input_usd_per_million_tokens is None
        or manifest.output_usd_per_million_tokens is None
    ):
        return None
    amount = (
        input_tokens * manifest.input_usd_per_million_tokens
        + output_tokens * manifest.output_usd_per_million_tokens
    ) / 1_000_000
    return round(amount, 10)


def compare_experiment_results(paths: list[str | Path]) -> dict[str, object]:
    """Compare complete runs only when their dataset, task cohort, and retry budgets match."""
    if len(paths) < 2:
        raise ValueError("comparison requires at least two experiment result files")
    dataset_hash: str | None = None
    task_cohort: tuple[str, ...] | None = None
    retry_budgets: tuple[int, ...] | None = None
    repetitions: int | None = None
    seen_run_ids: set[str] = set()
    records: list[dict[str, object]] = []
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"experiment result must be a JSON object: {path}")
        if payload.get("complete") is not True or payload.get("status", "complete") != "complete":
            raise ValueError(f"experiment result is incomplete: {path}")
        dataset = payload.get("dataset")
        current_hash = dataset.get("sha256") if isinstance(dataset, dict) else None
        if not isinstance(current_hash, str) or not current_hash:
            raise ValueError(f"experiment result is missing its dataset hash: {path}")
        if dataset_hash is None:
            dataset_hash = current_hash
        elif current_hash != dataset_hash:
            raise ValueError("experiments use different pinned datasets and cannot be compared")
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise ValueError(f"experiment result is missing manifest provenance: {path}")
        current_budgets_value = manifest.get("retry_budgets")
        if not isinstance(current_budgets_value, list) or any(
            not isinstance(value, int) or value < 0 for value in current_budgets_value
        ):
            raise ValueError(f"experiment result has invalid retry budgets: {path}")
        current_budgets = tuple(current_budgets_value)
        current_repetitions = manifest.get("repetitions")
        if not isinstance(current_repetitions, int) or current_repetitions < 1:
            raise ValueError(f"experiment result has invalid repetition count: {path}")
        if retry_budgets is None:
            retry_budgets = current_budgets
            repetitions = current_repetitions
        elif current_budgets != retry_budgets or current_repetitions != repetitions:
            raise ValueError("experiments use different retry budgets or repetition counts")
        result_records = payload.get("runs")
        if not isinstance(result_records, list) or not result_records:
            raise ValueError(f"experiment result contains no run records: {path}")
        current_tasks = tuple(
            sorted(
                {
                    str(record.get("task_id"))
                    for record in result_records
                    if isinstance(record, dict) and record.get("task_id")
                }
            )
        )
        if not current_tasks:
            raise ValueError(f"experiment result contains no stable task IDs: {path}")
        if task_cohort is None:
            task_cohort = current_tasks
        elif current_tasks != task_cohort:
            raise ValueError("experiments use different task cohorts and cannot be compared")
        for record in result_records:
            if not isinstance(record, dict):
                raise ValueError(f"experiment result contains an invalid run record: {path}")
            run_id = record.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError(f"experiment result contains a run without an ID: {path}")
            if run_id in seen_run_ids:
                raise ValueError(f"duplicate run ID in comparison inputs: {run_id}")
            seen_run_ids.add(run_id)
            records.append(record)

    grouped: dict[tuple[str, str, str, int], list[dict[str, object]]] = {}
    for record in records:
        key = (
            str(record.get("model") or "unknown"),
            str(record.get("architecture") or "unknown"),
            str(record.get("retrieval") or "unknown"),
            int(record.get("retrieval_limit", 12)),
        )
        grouped.setdefault(key, []).append(record)
    configurations = []
    for (
        model,
        architecture,
        retrieval,
        retrieval_limit,
    ), config_records in sorted(grouped.items()):
        configurations.append(
            {
                "configuration": {
                    "model": model,
                    "architecture": architecture,
                    "retrieval": retrieval,
                    "retrieval_limit": retrieval_limit,
                },
                "summary": summarize_from_records(config_records),
                "paired_task_outcomes": _paired_task_outcomes(config_records),
            }
        )
    return {
        "dataset_sha256": dataset_hash,
        "task_ids": list(task_cohort or ()),
        "retry_budgets": list(retry_budgets or ()),
        "repetitions": repetitions,
        "configurations": configurations,
        "interpretation": (
            "Exploratory comparison only; inspect task-level results and do not infer superiority "
            "from a small dataset."
        ),
    }


def _paired_task_outcomes(records: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for record in records:
        grouped.setdefault(str(record["task_id"]), []).append(record)
    return [
        {
            "task_id": task_id,
            "runs": len(task_records),
            "passed": sum(bool(record["passed"]) for record in task_records),
            "pass_rate": sum(bool(record["passed"]) for record in task_records) / len(task_records),
        }
        for task_id, task_records in sorted(grouped.items())
    ]


def run_experiment_sync(
    manifest_path: str | Path, dataset_path: str | Path, output_path: str | Path
) -> dict[str, object]:
    return asyncio.run(run_experiment(manifest_path, dataset_path, output_path))
