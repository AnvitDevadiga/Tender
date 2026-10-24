# Evaluation errata

Historical measurements are preserved exactly as recorded. This document
explains how to interpret them in light of defects found later. Nothing listed
here rewrites a historical file.

## 2026-10-24 — evaluator defects found at commit `5714455`

### E1. Verification workspaces were reused across attempts

`AgentWorkflow._verify_task` applied each attempt's cumulative patch to the same
verification directory. After a failed attempt, the next verification ran on top
of the earlier patch.

* **Affects:** runs with more than one attempt that reached task verification.
* **Published experiments:** checked on 2026-10-24. Every run in
  `groq-retry-ablation-3tasks-v1` (18) and `retrieval-cap-ab-v1` (60) recorded
  exactly one attempt, so no published result went through a reused verifier.
* **Fixed:** every verification check now runs in a fresh workspace materialized
  at the validated base commit.

### E2. Requested base commits were not enforced for API/worker runs

`RunService` recorded the repository HEAD but never compared it with
`issue.base_commit`. The benchmark runner did check out the pinned commit, so
benchmark records carry the correct `base_commit`. API-queued runs only ever
executed HEAD.

* **Fixed:** a mismatch is rejected before any provider call, and both requested
  and executed commits are recorded.

### E3. Tool allowlists were advisory

The allowlist filtered the tools *advertised* to a provider, but any tool name a
provider returned was executed. `run_command` was advertised by default to
providers without an allowlist.

* **Published experiments:** the Groq adapter declared an allowlist and the
  recorded `agent_tool.completed` events for the published runs show only
  `search_code`, `read_file`, and `replace_text`. No recorded call bypassed the
  allowlist, but the guarantee did not exist. One unpublished local development
  run (`3e3d95b0`, failed) executed `run_command` at 2026-10-09 15:39 UTC; that
  predates the commit that introduced the allowlist (`acfe9a6`, 15:59 UTC).
* **Fixed:** authorization is enforced at dispatch; refusals are recorded.

### E4. Host-side Git helpers could execute

Repository validation ran Git against the source's own metadata, and the
benchmark runner ran `git checkout` in a raw copy. A repository-controlled
`core.fsmonitor`, filter driver, or hook could execute on the host. The
published fixtures are Tender's own and contain none of these, so published
measurements are unaffected; the boundary was unsafe for anything else.

### E5. Credentials could follow HTTP redirects

`urllib` forwards `Authorization` to redirect targets. The GitHub client
rejected a cross-host redirect only after it had been followed. No incident is
known; redirects are now refused before they are followed.

### E6. `regression_free` did not measure regression preservation

Benchmark records set `regression_free` to the result of the independent
`verification_tests`, which are target-behaviour assertions. A `true` value
means "the independent target check passed on a clean copy", not "no existing
behaviour regressed". The dataset did not define a separate regression suite.
New records report `target` and `regression` outcomes separately, and
`regression` is `not_configured` unless regression commands exist.

### E7. Retrieval was labelled "BM25"

Manifests and records label the retriever `bm25`. The implementation is a
bounded lexical token-overlap ranker with a log-scaled term bonus and a Python
symbol-definition boost (`src/tender/retrieval.py`). It has no inverse document
frequency or document-length normalization and is not BM25. Historical records
keep the label they were written with; read `bm25` as "Tender lexical
retriever". New manifests use `lexical`.

### E8. The v1 public replay reconstructed data that had been recorded

`scripts/record_replay.py` (schema `tender.dashboard.replay.v1`) depended on the
git-ignored `results/artifacts/` directory, so a fresh clone could not rebuild
it. It also synthesized four timeline events per run instead of using the
recorded event streams, which existed in the local run databases. For the
follow-up run `2a5ce4e0` it went further. It copied configuration from another
run and showed retrieval `bm25` and sandbox `local`; the recorded values are
`text` and `docker`. It reported `tool_calls: 0`, where 14 were recorded, and a
creation time of `00:00:00Z`, where `15:57:11.986147Z` was recorded.

* **Fixed:** `scripts/export_replay_inputs.py` exported the recorded payloads and
  587 recorded events once, sanitizing only local artifact paths (each edit is
  listed). The replay (`tender.dashboard.replay.v2`) is rebuilt
  deterministically from that committed file, and every value carries a
  `recorded`, `derived`, or `unknown` label.
* **Unchanged:** the published experiment JSON and its measurements.

### E9. `/metrics` silently covered at most 1,000 runs

The endpoint aggregated `list_runs(1000)`, so totals, latency percentiles, and
pass rates were computed over the newest 1,000 runs without saying so.
**Fixed:** metrics are SQL aggregates over every run, and the response includes
`coverage`. Provider failures are reported separately, together with a pass rate
that excludes them.
