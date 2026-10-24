# Tender evaluation contract

This document defines what Tender is allowed to claim. Interpretation notes for
records written before this contract are in [Evaluation errata](EVALUATION_ERRATA.md).

## Required identity

Every new run records:

- issue source and issue text;
- repository identity, the **requested** base commit, and the **executed** base
  commit (they must match, or the run is rejected before any provider call);
- benchmark and dataset version (dataset SHA-256 for experiments);
- model/provider and configuration, including the integrity policy;
- agent architecture and retrieval strategy;
- commands executed, their exit codes, durations, and bounded output;
- the exact patch and its SHA-256;
- the event stream, stage durations, token counts, tool calls, and cost when
  the provider reports it;
- an evidence bundle (`evidence/manifest.json`) with a SHA-256 per file.

An issue without a pinned commit is not benchmark-grade. API runs accept it for
local development unless `TENDER_REQUIRE_PINNED_COMMIT=1`.

## Separate outcomes (`EvaluationOutcome`, schema `tender.outcome.v1`)

| Field | Values | Meaning |
| --- | --- | --- |
| `target` | passed / failed / not_run / error | The evaluator's target checks, run in a fresh workspace at the base commit with the candidate patch applied. |
| `regression` | passed / failed / not_configured / not_run / error | Separate regression checks in their own fresh workspace. `not_configured` means regression preservation was **not measured**. |
| `integrity` | passed / violated / not_checked | Protected paths, evaluator assets, and tool shadowing. |
| `environment` | ok / setup_failed / sandbox_unavailable / error | Setup commands or the sandbox failed; this is not a coding failure. |
| `provider` | ok / failed / not_used | A provider error (quota, auth, outage) is not a coding failure. |
| `timed_out` | bool | A command or the run-wide deadline expired. |
| `policy` | ok / rejected | Commit identity, repository safety, or tool authorization refused the run. |
| `independently_verified` | bool | The target checks passed in a pristine verifier. |
| `failed_stage` | planning / patch / agent_tests / static_analysis / diff_review / null | An agent-side gate stopped the run before independent verification; the candidate is rejected. |
| `overall` | accepted / rejected / error / inconclusive / pending_approval | Derived from the fields above. |

`overall = accepted` requires `target = passed`, `integrity = passed`,
`independently_verified = true`, and `regression` in {passed, not_configured}.
When regression is `not_configured`, reports must say that regression
preservation was not measured. A finite test suite never proves the absence of
regressions.

Legacy run status (`passed` / `failed`) and `failure_category` remain for
compatibility. Records written before this version have `outcome = null`.

## Integrity rules

Evaluator-owned files cannot decide the result. Patches touching protected paths
or shadowing the test tooling are rejected. Test files and test-runner
configuration are restored to base-commit content inside every verifier. Hidden
evaluator assets are written in after the patch and are never shown to the agent.
See [Security](SECURITY.md) for the residual limitation: the code under test runs
in the same process as the test runner.

## Experiments

- Trials are planned explicitly (`sequential`, seeded `randomized`, or
  `counterbalanced`), and the manifest text is snapshotted into the result.
- Provider and environment failures are **excluded** from solve rates and listed
  with a reason. Quota-aware stopping (`max_consecutive_provider_failures`) and
  a provider-call budget (`max_provider_calls`) bound live runs. Live providers
  also require `--authorize-live-provider`.
- Summaries report a Wilson 95% interval over included trials. Repeated trials
  of one task are not independent, so the interval understates uncertainty
  about new tasks.
- Comparisons require the same dataset hash, task cohort, budgets, and
  repetitions, and report matched task-level wins and losses with an exact sign
  test.

## Metrics

Metrics are derived from stored runs and events by SQL aggregates over every run;
the `/metrics` response states its `coverage`. They are never typed into README
files by hand. Any public result must include the manifest, dataset version, raw
run records, and the aggregation command. A provider that does not report cost
records cost as unknown, never as zero. Published-rate estimates are labelled as
estimates; Tender has no billing source.
