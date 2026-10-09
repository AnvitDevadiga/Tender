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

Earlier attempts exposed Groq's rejection of empty-root `list_directory` tool calls. ForgeBench removed that tool from the Groq tool menu, after which the provider used search/read tools correctly. The recorded failed attempts remain in the local SQLite store and artifact directory; they are development traces, not benchmark successes.

## Interpretation

This is evidence that API enqueueing, worker claiming, disposable-workspace creation, provider planning, and repository tools reached the live provider. It does not prove that generated code can pass tests: no patch reached the grader, clean-copy verifier, or completion gate. Do not report a solve rate or infer a cost from this run. Retry only when the provider quota permits another request; a free key does not guarantee enough TPM for the full prompt and tool loop.
