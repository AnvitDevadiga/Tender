# Provider Trial Record

Audit date: 2026-10-09

## Groq live run on the pinned fixture

The API accepted the ForgeCalc percentage task at base commit `6e1bb1322e0caffebdcd901a6c5bc253cd5b1a01`. The worker copied the fixture into a disposable workspace and used model `openai/gpt-oss-20b` through the configured Groq key. The model returned a valid implementation plan and successfully searched/read the source and test files.

| Field | Recorded value |
| --- | --- |
| Run ID | `df145ec9-7301-4ceb-ae7e-12bc61181a96` |
| Benchmark ID | `forgecalc-001-live-retry-bounded` |
| Final status | Failed before patch generation |
| Failure category | `model_tool_failure` |
| Input / output tokens | 6,274 / 477 |
| Reported cost | Unknown (`null`); provider returned no cost observation |
| Patch | Empty; no source change was applied |
| Failure | Groq HTTP 429 token-per-minute limit (8,000 TPM); retry delay was honored |

Earlier attempts exposed Groq's rejection of empty-root `list_directory` tool calls. Tender removed that tool from the Groq tool menu, after which the provider used search/read tools correctly. The recorded failed attempts remain in the local SQLite store and artifact directory; they are development traces, not benchmark successes.

## Interpretation

This is evidence that API enqueueing, worker claiming, disposable-workspace creation, provider planning, and repository tools reached the live provider. It does not prove that generated code can pass tests: no patch reached the grader, clean-copy verifier, or completion gate. Do not report a solve rate or infer a cost from this run. Retry only when the provider quota permits another request; a free key does not guarantee enough TPM for the full prompt and tool loop.

## Follow-up live run

On 2026-10-09, run `d1b6728a-ef97-4b93-8b6e-83fde7c4638a` used the narrowed percentage-only test command. The model changed only the percentage behavior and its task-specific pytest passed. The run then failed because `git diff --check HEAD` could not access Git metadata after sandbox grading. The gate now checks added patch lines directly.

Two follow-up runs after Docker access was restored, `acfdb30d-ab3d-4d59-92b5-2149929d62d9` and `58fa9007-6971-4b3c-a409-00ab7de56866`, failed plan parsing with zero usage. Their run configuration omitted the `groq/` provider prefix, so they selected the offline provider instead of Groq. Tender also now requests Groq JSON mode for planning, accepts a JSON object surrounded by commentary, and retries one malformed plan once while keeping schema and path validation. Groq documents JSON mode in its [Chat Completions API reference](https://console.groq.com/docs/api-reference).

The correctly selected provider run, `3f089cc3-f30b-49ff-98c6-fcc468e9f5a0`, used `groq/openai/gpt-oss-20b`, made the percentage fix, and passed the task-specific test. Its clean-copy check found that verification imported `percentage` from the package root, which does not export it. All fixture verification commands now import from `forgecalc.calculator`.

## Verified end-to-end task

Run `2a5ce4e0-ca02-4f85-9b7e-e541035b51e5` passed the complete local API-to-worker workflow: provider planning, repository edit, isolated targeted pytest, patch review, and the exact-patch verification on a separate clean copy.

| Field | Recorded value |
| --- | --- |
| Benchmark ID | `forgecalc-001-live-verified-followup` |
| Model | `groq/openai/gpt-oss-20b` |
| Final status | Passed, one attempt |
| Input / output tokens | 5,634 / 539 |
| Reported cost | Unknown (`null`); provider returned no cost observation |
| Patch | `percentage` now divides by 100; no unrelated fixture behavior changed |
| Target test | Passed, 1 test |
| Clean-copy verification | Passed |

This is a functional smoke trial, not a benchmark score. One task and one run do not establish solve rate, regression rate, speed, or cost. Repeated trials over a larger task set remain necessary for those claims.


## Repeated Groq fixture trial (2026-10-10)

Manifest: [`groq-retry-ablation-3tasks-v1.toml`](../experiments/groq-retry-ablation-3tasks-v1.toml). Raw, versioned records: [experiment JSON](experiments/groq-retry-ablation-3tasks-v1.json). The pinned development dataset has three synthetic ForgeCalc tasks; each was run twice at retry budgets 0, 1, and 2 (18 completed trials). Model: `groq/openai/gpt-oss-20b`; single-agent workflow; BM25 retrieval; local trusted-fixture sandbox.

| Measurement | Result |
| --- | ---: |
| Completed / passed trials | 18 / 18 (100%) |
| Unique tasks / repetitions | 3 / 2 per retry budget |
| Regression-free verification | 18 / 18 (100%) |
| Average workflow latency | 52.62 seconds |
| Input tokens | 139,576 total |
| Output tokens | 15,200 total |
| Attempts | 1 per run on average; retry recovery not exercised |
| Provider-reported cost | Unknown (`null`) |
| Published-rate estimate | $0.01503 total for 18 runs |

The cost estimate applies Groq's published GPT-OSS 20B rates ($0.075 per million input tokens and $0.30 per million output tokens) to recorded token counts; it is not a billing record. Rates and account limits can change. See Groq's [model pricing](https://console.groq.com/docs/model/openai/gpt-oss-20b) and [rate limits](https://console.groq.com/docs/rate-limits).

**Interpretation:** this is a useful repeatability/measurement pipeline check, not a credible broad solve-rate estimate. All three tasks are small, synthetic, and drawn from the same fixture repository; the 18 observations are clustered repeats, not 18 independent tasks. The run only checks the retry-budget conditions because every task passed on its first attempt. It cannot support claims about regressions on larger repositories, retry recovery, model comparisons, or arbitrary coding work. Grow the pinned dataset substantially and repeat the experiment before making such claims.
