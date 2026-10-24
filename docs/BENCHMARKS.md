# Benchmarks

Tender benchmarks are intended to be reproducible repository-level software tasks. The existing local fixture and JSONL runner form an experimental starting point; they are not a broad public benchmark and do not establish model superiority.

## Current format and runner

Task JSONL schema v2 requires a stable `task_id`, issue, local workspace, full 40-character `base_commit`, setup commands, agent test commands, independent verification commands, and a per-task timeout. An optional `static_analysis_commands` array adds lint or other static checks. The issue's optional base commit must match the task pin. The runner verifies the resolved checkout SHA before execution, bounds setup and agent execution by task/manifest budgets, and stops at the experiment-wide `max_minutes` budget. A budget-truncated output is marked `complete: false`.

The `run-experiment` runner supports the local sandbox for trusted fixture datasets and the Docker sandbox for anything else. Do not use it to execute arbitrary public datasets. Manifests that route to a billable provider (`groq/…`, `openai/…`) must set `max_provider_calls`, and the command must be given `--authorize-live-provider`. Manifests can also set `trial_order` (`sequential`, `randomized` with `seed`, or `counterbalanced`) and `max_consecutive_provider_failures` for quota-aware stopping. Tasks may add `regression_commands`; without them, records report regression as `not_configured`. Setup commands execute in the run's disposable copy before its agent tests. The separate API workflow requires Docker mode; setup and test commands share one disposable container per attempt, which is removed after grading. Local image build and sandbox smoke checks have succeeded. A small repeated Groq experiment now exists: 18 completed trials over three unique synthetic tasks, two repetitions for retry budgets 0/1/2. See [provider trial record](PROVIDER_TRIALS.md) and the [raw experiment JSON](experiments/groq-retry-ablation-3tasks-v1.json). Treat it as exploratory only.

The agent records a concise provider-generated plan, then runs target tests, built-in diff checks, optional static-analysis commands, deterministic patch review, and verification on a separate clean copy. The experiment runner performs an additional task-owned verification pass and records its output. The fixture verification checks are separate assertions stored in the pinned dataset. `regression_free_rate` uses target-test-passing runs as its denominator; runs that fail before verification are not counted as regression observations.

Run the fixture experiment after installing the package:

```bash
python -m tender validate-benchmark --manifest experiments/retry-ablation-v1.toml --dataset benchmarks/tasks.jsonl
python -m tender run-experiment --manifest experiments/retry-ablation-v1.toml --dataset benchmarks/tasks.jsonl --output results/fixture-retry-ablation.json
```

Do not treat a command's completion as a successful benchmark claim without inspecting each run and its artifacts. Provider-free deterministic mode is a failure-path baseline, not a coding-capability baseline.

Compare result files with `python -m tender compare --results a.json b.json --output comparison.json`. The command rejects incomplete files, mismatched dataset hashes, task cohorts, retry budgets or repetition counts, and duplicate run IDs. It groups raw runs by model, architecture, and retrieval configuration, reports paired task outcomes, and includes a small-sample warning. It does not compute uncertainty or prove statistical significance.

Result files include a schema version, complete/partial status, manifest and dataset hashes, trial design, run records, and summaries broken down by retry budget. Historical files that lack required trial provenance cannot be compared under the current contract.

The checked-in `results/fixture-retry-ablation.json` is a historical development artifact with zero successful runs. Keep it for traceability. The newer repeated experiment has 18/18 passes, but only three unique synthetic tasks; do not generalize its result.

## Required reproducibility record

The next schema version must pin repository URL/identity, full base commit, task statement, setup commands, target tests, evaluator-owned verification tests, runtime image/dependency lock, resource budgets, dataset/task version, and hashes. It must execute from a clean copy and fail if the requested commit or setup identity is unavailable. The agent must not modify evaluator-owned files.

Each run preserves the task ID and checked-out commit with configuration, command/output records, patch, test outcomes, and manifest/dataset hashes. Publish raw artifacts and aggregation command with any reported metric.

## Comparison limits

Report task count, repetitions, resolved count, resolution rate, failure categories, cost observations, latency, iterations, retries, tool calls, tokens, patch size, and regression outcomes. Separate unknown costs from known costs. Small datasets should be labeled exploratory; do not imply statistically meaningful differences without adequate task coverage and uncertainty analysis.

## Experiment records (schema 3)

New result files snapshot the manifest text and record, per trial, the
`trial_index`, separate `target_outcome` and `regression_outcome`,
`independent_final_verification`, the full `outcome`, exclusions
(`provider_failure`, `environment_failure`) with reasons, stage timings, and
counters. They also record `stop_reason` and planned versus executed trials.
Historical schema 2 files are unchanged; see the [errata](EVALUATION_ERRATA.md).
The retriever label `bm25` in historical manifests refers to Tender's lexical
retriever, which is not BM25. New manifests should say `lexical`.
