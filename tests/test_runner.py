import json
import subprocess

import pytest

from tender.experiments import BenchmarkTask
from tender.providers import ModelResponse
from tender.runner import (
    compare_experiment_results,
    run_experiment_sync,
    summarize_from_records,
    verify_final_patch,
)


def test_benchmark_task_loads_jsonl(tmp_path) -> None:
    # The fixture's immutable revision is used to validate schema loading.
    import subprocess

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task_id": "test-1",
                "issue": {"repository": "o/r", "title": "bug", "base_commit": commit},
                "workspace": str(repository),
                "base_commit": commit,
                "setup_commands": [],
                "test_commands": ["true"],
                "verification_tests": ["true"],
                "timeout_seconds": 60,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tasks = BenchmarkTask.load_jsonl(path)
    assert len(tasks) == 1
    assert tasks[0].commands == ("true",)
    assert tasks[0].task_id == "test-1"
    assert tasks[0].base_commit == commit


def test_benchmark_task_rejects_missing_commit_pin(tmp_path) -> None:
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task_id": "unpinned",
                "issue": {"repository": "o/r", "title": "bug"},
                "workspace": str(tmp_path),
                "base_commit": "PINNED_COMMIT",
                "setup_commands": [],
                "test_commands": ["true"],
                "verification_tests": ["true"],
                "timeout_seconds": 60,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="full 40-character"):
        BenchmarkTask.load_jsonl(path)


def test_verifier_applies_agent_patch_to_a_separate_clean_copy(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "calculator.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=base, check=True)
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=base, check=True)

    agent_copy = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(base), str(agent_copy)], check=True)
    (agent_copy / "calculator.py").write_text("def answer():\n    return 2\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff", "--no-ext-diff", "HEAD"],
        cwd=agent_copy,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    passed, results = verify_final_patch(
        base,
        tmp_path / "verified",
        patch,
        (),
        ("python3 -c 'from calculator import answer; assert answer() == 2'",),
        10,
    )

    assert passed is True
    assert results[0]["passed"] is True
    assert (base / "calculator.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert (
        (tmp_path / "verified" / "calculator.py").read_text(encoding="utf-8").endswith("return 2\n")
    )


def test_regression_free_rate_uses_agent_passes_as_denominator() -> None:
    summary = summarize_from_records(
        [
            {
                "task_id": "task-1",
                "passed": False,
                "target_tests_passed": False,
                "regression_free": None,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": 1,
                "cost_usd": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "failure_category": "test",
            },
            {
                "task_id": "task-1",
                "passed": True,
                "target_tests_passed": True,
                "regression_free": True,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": 1,
                "cost_usd": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "failure_category": "none",
            },
            {
                "task_id": "task-2",
                "passed": False,
                "target_tests_passed": True,
                "regression_free": False,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": 1,
                "cost_usd": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "failure_category": "test",
            },
        ]
    )

    assert summary["regression_free_rate"] == 0.5
    assert summary["regression_evaluations"] == 2
    assert summary["unique_tasks"] == 2
    assert summary["sample_size_warning"]


def test_retry_budget_summary_reports_latency_and_task_sample_size() -> None:
    summary = summarize_from_records(
        [
            {
                "task_id": "task-1",
                "passed": True,
                "target_tests_passed": True,
                "regression_free": True,
                "attempts": 2,
                "retry_budget": 1,
                "latency_ms": 100,
                "cost_usd": None,
                "input_tokens": 1,
                "output_tokens": 1,
                "failure_category": "none",
            },
            {
                "task_id": "task-1",
                "passed": False,
                "target_tests_passed": False,
                "regression_free": None,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": None,
                "cost_usd": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "failure_category": "test",
            },
        ]
    )

    assert summary["by_retry_budget"]["1"]["average_latency_ms"] == 100
    assert summary["by_retry_budget"]["1"]["unique_tasks"] == 1
    assert summary["by_retry_budget"]["0"]["average_latency_ms"] is None


def test_summary_separates_actual_cost_from_published_rate_estimate() -> None:
    summary = summarize_from_records(
        [
            {
                "task_id": "task-1",
                "passed": True,
                "target_tests_passed": True,
                "regression_free": True,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": 100,
                "cost_usd": None,
                "published_rate_cost_estimate_usd": 0.0005,
                "input_tokens": 5_000,
                "output_tokens": 500,
                "failure_category": "none",
            }
        ]
    )

    assert summary["total_cost_usd"] is None
    assert summary["total_published_rate_cost_estimate_usd"] == 0.0005
    assert summary["published_rate_estimate_observations"] == 1


def test_summary_ignores_provider_failures_and_warns_for_small_sample() -> None:
    summary = summarize_from_records(
        [
            {
                "task_id": "task-1",
                "repository": "repo-a",
                "passed": True,
                "attempts": 1,
                "retry_budget": 0,
                "retrieval_limit": 6,
                "latency_ms": 100,
                "cost_usd": None,
                "input_tokens": 100,
                "output_tokens": 10,
                "failure_category": "none",
            },
            {
                "task_id": "task-2",
                "repository": "repo-a",
                "passed": False,
                "attempts": 1,
                "retry_budget": 0,
                "retrieval_limit": 6,
                "latency_ms": 50,
                "cost_usd": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "failure_category": "provider_error",
            },
        ]
    )

    assert summary["provider_failures"] == 1
    assert summary["solve_rate_excluding_provider_failures"] == 1.0
    assert summary["unique_repositories"] == 1
    assert summary["sample_size_warning"]
    assert summary["by_retrieval_limit"]["6"]["provider_failures"] == 1


def test_offline_experiment_runs_setup_patch_and_independent_verification(
    tmp_path, monkeypatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "app.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()

    manifest = tmp_path / "experiment.toml"
    manifest.write_text(
        "\n".join(
            [
                'name = "offline-integration"',
                'dataset = "offline-fixture-v1"',
                'model = "fixture/static-patch"',
                'architecture = "single-agent"',
                'retrieval = "lexical"',
                "repetitions = 1",
                "max_tokens = 128",
                "max_minutes = 1",
                "retry_budgets = [0]",
                'sandbox = "local"',
                "timeout_seconds = 10",
            ]
        ),
        encoding="utf-8",
    )
    task = {
        "schema_version": 2,
        "task_id": "offline-001",
        "issue": {
            "repository": "local/repository",
            "title": "Return two",
            "body": "answer() should return 2",
            "base_commit": commit,
        },
        "workspace": str(repository),
        "base_commit": commit,
        "setup_commands": [
            'python3 -c \'from pathlib import Path; Path("setup_marker").write_text("ready")\''
        ],
        "static_analysis_commands": ["python3 -c 'assert True'"],
        "test_commands": [
            "python3 -c 'from app import answer; assert answer() == 2; "
            'assert open("setup_marker").read() == "ready"\''
        ],
        "verification_tests": ["python3 -c 'from app import answer; assert answer() == 2'"],
        "timeout_seconds": 10,
    }
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps(task) + "\n", encoding="utf-8")

    class StaticPatchProvider:
        async def generate(self, messages, tools=None):
            if "implementation plan" in messages[0]["content"].lower():
                return ModelResponse(
                    '{"summary":"Return the requested value","steps":["Update answer"],'
                    '"target_files":["app.py"]}',
                    input_tokens=4,
                    output_tokens=6,
                )
            return ModelResponse(
                "```diff\n--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
                " def answer():\n-    return 1\n+    return 2\n```",
                input_tokens=8,
                output_tokens=12,
            )

    monkeypatch.setattr("tender.runner.provider_for", lambda config: StaticPatchProvider())
    output = run_experiment_sync(manifest, dataset, tmp_path / "output" / "result.json")

    record = output["runs"][0]
    assert output["complete"] is True
    assert record["passed"] is True
    assert record["regression_free"] is True
    assert record["input_tokens"] == 12
    assert record["output_tokens"] == 18
    assert record["cost_usd"] is None
    assert output["summary"]["regression_free_rate"] == 1.0
    assert output["summary"]["sample_size_warning"]
    assert (repository / "app.py").read_text(encoding="utf-8").endswith("return 1\n")

    artifact_dir = tmp_path / "output" / "artifacts" / record["run_id"]
    verification = json.loads((artifact_dir / "verification.json").read_text(encoding="utf-8"))
    result = json.loads((artifact_dir / "result.json").read_text(encoding="utf-8"))
    assert verification["passed"] is True
    assert result["passed"] is True


def test_model_comparison_requires_complete_runs_on_the_same_dataset(tmp_path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = {
        "complete": True,
        "status": "complete",
        "manifest": {"retry_budgets": [0], "repetitions": 1},
        "dataset": {"sha256": "same-pinned-dataset"},
        "runs": [
            {
                "run_id": "run-a",
                "task_id": "task-1",
                "model": "provider/model-a",
                "architecture": "single-agent",
                "retrieval": "lexical",
                "passed": True,
                "target_tests_passed": True,
                "regression_free": True,
                "attempts": 1,
                "retry_budget": 0,
                "latency_ms": 100,
                "patch_bytes": 25,
                "cost_usd": None,
                "input_tokens": 10,
                "output_tokens": 20,
                "tool_calls": 2,
                "failure_category": "none",
            }
        ],
    }
    first.write_text(json.dumps(common), encoding="utf-8")
    alternate = json.loads(json.dumps(common))
    alternate["runs"][0]["run_id"] = "run-b"
    alternate["runs"][0]["model"] = "provider/model-b"
    alternate["runs"][0]["passed"] = False
    alternate["runs"][0]["regression_free"] = False
    second.write_text(json.dumps(alternate), encoding="utf-8")

    comparison = compare_experiment_results([first, second])

    assert comparison["dataset_sha256"] == "same-pinned-dataset"
    assert comparison["task_ids"] == ["task-1"]
    assert comparison["configurations"][0]["paired_task_outcomes"][0]["runs"] == 1
    assert len(comparison["configurations"]) == 2
    assert comparison["interpretation"].startswith("Exploratory comparison")

    alternate["dataset"]["sha256"] = "different-dataset"
    second.write_text(json.dumps(alternate), encoding="utf-8")
    with pytest.raises(ValueError, match="different pinned datasets"):
        compare_experiment_results([first, second])

    alternate["dataset"]["sha256"] = "same-pinned-dataset"
    alternate["complete"] = False
    alternate["status"] = "partial"
    second.write_text(json.dumps(alternate), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        compare_experiment_results([first, second])


def test_model_comparison_rejects_mismatched_task_cohorts(tmp_path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = {
        "complete": True,
        "status": "complete",
        "manifest": {"retry_budgets": [0], "repetitions": 1},
        "dataset": {"sha256": "same"},
        "runs": [{"run_id": "run-a", "task_id": "task-1"}],
    }
    first.write_text(json.dumps(common), encoding="utf-8")
    alternate = json.loads(json.dumps(common))
    alternate["runs"][0]["run_id"] = "run-b"
    alternate["runs"][0]["task_id"] = "task-2"
    second.write_text(json.dumps(alternate), encoding="utf-8")

    with pytest.raises(ValueError, match="different task cohorts"):
        compare_experiment_results([first, second])


def test_model_comparison_rejects_mismatched_retry_design(tmp_path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    common = {
        "complete": True,
        "status": "complete",
        "manifest": {"retry_budgets": [0], "repetitions": 1},
        "dataset": {"sha256": "same"},
        "runs": [{"run_id": "run-a", "task_id": "task-1"}],
    }
    first.write_text(json.dumps(common), encoding="utf-8")
    alternate = json.loads(json.dumps(common))
    alternate["runs"][0]["run_id"] = "run-b"
    alternate["manifest"]["repetitions"] = 2
    second.write_text(json.dumps(alternate), encoding="utf-8")

    with pytest.raises(ValueError, match="different retry budgets or repetition counts"):
        compare_experiment_results([first, second])
