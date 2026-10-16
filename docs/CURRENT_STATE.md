# Current State

Audit date: 2026-10-16 (hardening pass: cancellation, pagination, provider breadth, request bounds, retry classification, graceful worker stop).

## Where the project is

Tender is a local-first coding-agent evaluation prototype. The current phase is **hardening the local run loop and preparing a safe portfolio demo**. It is not a public coding service. It now has one exploratory repeated AI experiment, which must not be presented as broad benchmark performance.

Think of it as a workshop: a task and a repository go in, a model may suggest a patch, tests check the patch, and the system records what happened. The checks can run without a model key; real model-generated patches require an optional provider key such as `GROQ_API_KEY`.

## Implemented

- FastAPI endpoints, Pydantic request/result models, and SQLite persistence with append-only run events.
- A deterministic workflow state machine persists task, setup, repository analysis, retrieval, provider-generated implementation plans, patching, tests, quality gates, clean-copy verification, retry, completion, and failure transitions.
- Plans are schema-validated, bounded, and restricted to repository-contained file paths. The patch request receives the validated plan as untrusted context.
- A bounded structured tool loop exposes validated repository file/search/Git/dependency operations plus sandboxed commands, tests, and configured lint. It blocks secrets, symlinks, evaluator and benchmark files, records redacted inputs and bounded outputs, and limits tool rounds/calls.
- The provider can call typed tools for bounded file reads/writes, exact replacements, lexical search, directory listing, Git inspection, dependency lookup, tests, lint, and sandboxed commands. File operations block escapes, symlinks, credentials, and evaluator/benchmark files; commands execute through the selected sandbox.
- Groq compatibility exposes only repository search/read/write tools; directory listing and command execution stay out of its tool menu. After a repository edit, Tender ends the provider loop and runs its own configured quality gates.
- Built-in patch whitespace checking inspects added lines directly so it still works when temporary Git metadata is unavailable after Docker grading.
- Tool arguments are centrally validated and audit events redact file contents to byte counts and hashes. Tool calls, rounds, and captured outputs are bounded.
- A passing run must clear built-in patch whitespace checking, optional configured static-analysis commands, deterministic patch-scope/size review, and the exact-patch test run on a second clean repository copy.
- Optional final approval is available through an API route protected by a server-side bearer token; decisions and terminal state changes are stored atomically. This gate is not configured by default.
- Coding runs require a clean, standalone Git repository. The base commit is recorded before the disposable agent and verifier copies are created.
- Offline deterministic provider and optional Groq provider adapter.
- Bounded lexical repository retrieval with auditable selection reasons, capped Python AST matching for named functions/classes, and a compact repository map.
- Unified-diff patching, bounded retry loop, configured-command grading, and run artifacts.
- Disposable bounded workspaces for API workflow runs. API workflow requests must stay under an operator-configured root and use Docker mode.
- Docker grading uses one container per attempt, with network disabled, CPU/memory/PID/time limits, reduced capabilities, non-root execution, bounded temporary storage, and cleanup on completion or timeout. Setup and test commands share that attempt container.
- Local and Docker command runners cap captured output; local command timeouts terminate the command process group.
- GitHub issue URL validation and bounded API response handling.
- Versioned benchmark tasks with stable IDs, full commit pins, setup/test/verification commands, and time limits.
- Optional request idempotency keys and atomic SQLite run claiming. Replaying a key returns the stored run as-is — including one that has since been cancelled — without re-executing or resurrecting it.
- API run creation atomically persists a queued run and audit event, then returns immediately. A separate worker claims the oldest job, rechecks its relative workspace beneath the configured root, and executes it.
- Queued runs can be cancelled atomically through `POST /runs/{id}/cancel`; the transition, audit events, and metadata are committed together, and worker claims skip cancelled runs. Cancelled runs are reported separately from completed runs in metrics.
- Run listing supports status filtering, limits, and offsets, plus a `summary=true` mode that drops patch, test-output, and issue-body payloads. Event timelines accept limit/offset pagination and return up to 5000 events per page by default (20000 maximum). Metrics include latency percentiles and per-model/per-benchmark summaries.
- Request bodies are capped (`TENDER_MAX_REQUEST_BYTES`, default 1 MiB) before routing, issue/config payload fields are length-bounded, and GitHub issue imports are constrained by the validated `Issue` model. Persisted payloads that predate a bound (including legacy number encodings, empty required strings, and invalid sandbox values) are clamped to the current limits when read, so legacy rows stay readable and are healed in place the next time a worker claims and saves them.
- Mutating API routes (`POST /runs`, `POST /runs/{id}/cancel`) accept an optional server-configured bearer token (`TENDER_API_TOKEN`); when it is unset the local-development API stays open and must remain bound to loopback.
- Groq and any OpenAI-compatible chat-completions endpoint (OpenAI, OpenRouter, vLLM, Ollama, ...) are supported through one provider seam. Reasoning-style models (o-series, the GPT-5 family, or any endpoint that rejects an explicit temperature) omit the temperature override. Provider retries cover HTTP 429 (honoring the advertised delay), transient 5xx responses, and network timeouts; permanent 4xx errors fail fast.
- The worker responds to SIGINT/SIGTERM by finishing the in-flight run before exiting (a second stop signal exits immediately so a stuck run can be abandoned), spaces out lease-recovery scans to avoid idle write-lock churn, and the workflow's stored test output is bounded while full gate output remains in run artifacts.
- Local Compose separates API, worker, and sandbox images. Only the worker receives the Docker socket; SQLite state, artifacts, fixtures, and a host-visible temporary directory are shared. Workers maintain expiring database leases, heartbeat while running, and fail expired runs without replay; stale owners are fenced from writing run state.
- Benchmark verification applies the final agent patch to a second clean task copy; separate fixture assertions support regression-free-rate measurement. Versioned experiment output includes manifest/dataset provenance, trial design, per-retry-budget latency/attempt summaries, patch size, and warnings for fewer than 30 unique tasks. Comparisons require complete experiments with matching dataset hash, task cohort, retry budgets, and repetition count, and report per-task outcomes.
- The dashboard is a zero-build command-center explorer (`dashboard/index.html` + `styles.css` + one ES module `app.js`, JSDoc-typed and verified with `tsc --checkJs` in strict mode via `dashboard/jsconfig.json`): status/model/benchmark filters with counts, sorting, debounced search, keyboard shortcuts and a ⌘K command palette, a tabbed run inspector (overview, timeline, patch diff, test output, artifacts) with queued-run cancellation, up to four-run comparison, JSON/CSV export, shareable URL state, light/dark themes, skeleton/error/empty states, and adaptive live refresh. It reads the summary list endpoint plus the `X-Total-Count` header for honest pagination. CI workflow, setup/deployment notes, and a separate static portfolio page with a GitHub Pages workflow remain in place.

## Not implemented yet

- LangGraph orchestration, MCP interoperability, dependency-graph expansion, symbol-aware retrieval outside Python, and plan approval.
- Plan approval, GitHub PR creation, and role-based approval identity/permissions.
- PostgreSQL/migrations, workflow checkpoint resume, and public-run identity, authorization, per-user quotas, and distributed rate limits. The optional `TENDER_API_TOKEN` gate is a server-side shared secret, not multi-user auth, and the in-process token-bucket limiter (`TENDER_RATE_LIMIT_PER_MINUTE`) only throttles mutating routes within a single process.
- Broad, independently sampled benchmark evidence. One small repeated Groq fixture experiment completed; see [provider trial record](PROVIDER_TRIALS.md).
- The project is connected to the supplied public GitHub repository. Source is pushed to `main`; the interactive recorded-run demo is live at [anvitdevadiga.github.io/Tender](https://anvitdevadiga.github.io/tender/).

## Verification

- `pytest -q`: 149 passed, with one Starlette/httpx deprecation warning. The hardening pass added 41 regression tests covering cancellation, pagination, request caps, API-token gating, provider selection, retry classification, legacy-payload clamping, reasoning-model temperature handling, worker stop signals, rate limiting, count headers, and security headers — plus three dashboard smoke tests covering static asset serving, JavaScript/CSS content types, and the no-inline-code CSP contract.
- A static-serving regression was found and fixed while adding the dashboard tests: the request-size middleware replayed `http.disconnect` immediately after a buffered body, which made streaming responses (dashboard assets) send headers only and truncate to empty bodies. It now hands the receive channel back to the server after replay.
- `npx tsc -p dashboard/jsconfig.json`: clean under `strict` + `checkJs` (no output).
- `ruff check src tests` and `ruff format --check src tests`: passed.
- `python3 -m compileall -q src tests`: passed.
- GitHub Actions CI runs Ruff lint/format, compilation, the full test suite, and benchmark validation.
- Benchmark manifest validation: three pinned task definitions accepted.
- `python scripts/prepare_fixtures.py` creates the same pinned baseline from a clean clone; the pin test and manifest validation pass locally.
- Latest worker and sandbox images rebuilt successfully; the local API health endpoint returned HTTP 200, and the corrected Groq-backed task passed end to end.
- GitHub Pages deployment workflow succeeded; the published page returned HTTP 200 and serves the recorded-run demo (latest code deployment pending).
- A real Groq-backed task completed end to end through the local API and worker: the model produced the percentage fix, the isolated target test passed, and the exact patch passed clean-copy verification. It used 5,634 input and 539 output tokens; the provider returned no cost, so cost is unknown. This is a smoke trial, not a benchmark score. See [provider trial record](PROVIDER_TRIALS.md).
- An exploratory repeated Groq run completed 18/18 trials across three synthetic tasks. Regression-free verification passed in all 18. Average latency was 52.62 seconds; tokens totaled 139,576 input and 15,200 output. Provider-reported cost is unknown; a published-rate calculation estimates $0.01503 total. This sample is too small and clustered for general performance claims. Full records: [experiment JSON](experiments/groq-retry-ablation-3tasks-v1.json).

## Demo and cost

The portfolio page in `demo/index.html` uses the Tender name and a monochrome editorial design. It interactively replays a recorded successful run and repeated-trial summary; it does not accept tasks or run submitted code. It is published through GitHub Pages at [the current Pages address](https://anvitdevadiga.github.io/tender/). The custom domain remains deferred.

Local tests and manifest checks need no API key. A real model call needs a provider key and is subject to that provider's current limits and billing terms. Public interactive code execution is intentionally not available until authentication, quotas, worker separation, persistence, and stronger isolation are built and tested.
