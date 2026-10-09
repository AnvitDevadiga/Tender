# Evaluation

## Meaning of success

A run passes only when target tests, built-in diff checks, configured static-analysis commands, deterministic patch review, and exact-patch verification on a second clean copy succeed; protected evaluator files must remain intact. A provider response or generated patch is not success. The benchmark runner performs an additional task-owned verification pass and records its output. A verifier failure changes the benchmark outcome to failed even when the agent tests passed.

## Available measurements

Stored runs can provide pass/fail status, attempts, latency, input/output tokens, provider-reported cost when available, tool-call totals, failure category, command results, and patch artifacts. `/metrics` summarizes up to the most recent 1,000 stored runs. These values describe only recorded runs in that local store and must not be generalized beyond their sample.

Benchmark experiment output also includes task ID, checked-out commit, retry budget, repetition, run ID, result, duration, final patch size, token usage, cost, verification outcomes, and tool-call count, plus manifest and dataset hashes and the trial design (model, architecture, retrieval, repetition count, retry budgets, and limits). `regression_free_rate` is calculated over runs that passed agent tests and reached independent verification; failed or unverified runs are excluded from that denominator. Fewer than 30 unique tasks are explicitly marked exploratory. Manifest-wide budget exhaustion marks output incomplete and `status: partial`. Cost is `null` when unknown. A zero cost is valid only when the provider explicitly reports a zero amount.

The comparison command accepts only complete result files with matching dataset hashes, task cohorts, retry budgets, and repetition counts, and unique run IDs. It groups records by model, architecture, and retrieval configuration; it reports task-level outcomes and does not claim statistical significance. A benchmark-level verifier can fail after the agent workflow reaches its terminal state, so inspect the separate verification event and final run status.

## Limitations

- The comparison does not estimate uncertainty or establish statistical significance.
- The experiment CLI is currently local-only for trusted fixture datasets. API workflow commands use Docker; Docker image builds, a sandbox smoke test, and one live worker execution have succeeded locally.
- A passing configured command is only as strong as the benchmark's evaluator. Verification tests must be independent and protected from agent modification.
- Local fixture runs are not evidence about performance on arbitrary GitHub repositories.
- A real Groq-backed task reached planning and repository search/read but hit the provider's free-tier TPM limit before producing a patch. The 97 passing tests are ForgeBench's own tests, not agent resolution results. See [provider trial record](PROVIDER_TRIALS.md); it is not an AI performance result.
- The checked-in `results/fixture-retry-ablation.json` is a historical development run with zero passes and provider/tool failures. It is retained as an audit artifact, not a performance claim.

## Reporting rules

Report the exact command, task count, dataset/manifest hashes, pinned commits, run IDs, raw artifacts, and known limitations. Include all failed runs and categorize them. Do not hard-code or hand-edit performance numbers. Keep unknown cost separate from zero and describe small-sample results as exploratory.
