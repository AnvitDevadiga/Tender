# Current State

Audit date: 2026-10-09

## Where the project is

ForgeBench is a local-first coding-agent evaluation prototype. The current phase is **hardening the local run loop and preparing a safe portfolio demo**. It is not a public coding service and it has not produced a verified AI benchmark result.

Think of it as a workshop: a task and a repository go in, a model may suggest a patch, tests check the patch, and the system records what happened. The checks can run without a model key; real model-generated patches require an optional provider key such as `GROQ_API_KEY`.

## Implemented

- FastAPI endpoints, Pydantic request/result models, and SQLite persistence with append-only run events.
- A deterministic workflow state machine persists task, setup, repository analysis, retrieval, provider-generated implementation plans, patching, tests, quality gates, clean-copy verification, retry, completion, and failure transitions.
- Plans are schema-validated, bounded, and restricted to repository-contained file paths. The patch request receives the validated plan as untrusted context.
- A bounded structured tool loop exposes validated repository file/search/Git/dependency operations plus sandboxed commands, tests, and configured lint. It blocks secrets, symlinks, evaluator and benchmark files, records redacted inputs and bounded outputs, and limits tool rounds/calls.
- The provider can call typed tools for bounded file reads/writes, exact replacements, lexical search, directory listing, Git inspection, dependency lookup, tests, lint, and sandboxed commands. File operations block escapes, symlinks, credentials, and evaluator/benchmark files; commands execute through the selected sandbox.
- Groq compatibility omits directory listing from its tool menu because Groq rejects some malformed root-path calls before ForgeBench can validate them. Provider HTTP 429 retries honor the suggested delay, bounded to 60 seconds and three retries per request.
- Tool arguments are centrally validated and audit events redact file contents to byte counts and hashes. Tool calls, rounds, and captured outputs are bounded.
- A passing run must clear built-in `git diff --check`, optional configured static-analysis commands, deterministic patch-scope/size review, and the exact-patch test run on a second clean repository copy.
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
- Optional request idempotency keys and atomic SQLite run claiming.
- API run creation atomically persists a queued run and audit event, then returns immediately. A separate worker claims the oldest job, rechecks its relative workspace beneath the configured root, and executes it.
- Local Compose separates API, worker, and sandbox images. Only the worker receives the Docker socket; SQLite state, artifacts, fixtures, and a host-visible temporary directory are shared. Workers maintain expiring database leases, heartbeat while running, and fail expired runs without replay; stale owners are fenced from writing run state.
- Benchmark verification applies the final agent patch to a second clean task copy; separate fixture assertions support regression-free-rate measurement. Versioned experiment output includes manifest/dataset provenance, trial design, per-retry-budget latency/attempt summaries, patch size, and warnings for fewer than 30 unique tasks. Comparisons require complete experiments with matching dataset hash, task cohort, retry budgets, and repetition count, and report per-task outcomes.
- Local run explorer dashboard, CI workflow, setup/deployment notes, and a separate static portfolio page with a GitHub Pages workflow.

## Not implemented yet

- LangGraph orchestration, MCP interoperability, dependency-graph expansion, symbol-aware retrieval outside Python, and plan approval.
- Plan approval, GitHub PR creation, and role-based approval identity/permissions.
- PostgreSQL/migrations, workflow checkpoint resume, cancellation, and public-run authentication/quotas.
- Repeated, measured AI benchmark results. Existing fixture/test results are engineering checks, not model success metrics.
- This checkout currently has no `.git` metadata, so its changes cannot yet be compared with or pushed to the supplied GitHub repository from here.

## Verification

- `python3 -m pytest -q`: 97 passed, with one Starlette/httpx deprecation warning.
- `python3 -m compileall -q src tests`: passed.
- Ruff is not installed in the current environment; lint and format checks are not verified in this audit.
- Benchmark manifest validation: three pinned task definitions accepted.
- Docker images built; the API and worker containers are running, and `GET /health` returned HTTP 200. A network-disabled sandbox smoke test succeeded.
- A real Groq-backed task on the pinned fixture reached planning and repository search/read, but Groq returned HTTP 429 before code generation. No patch was applied. Cost is unknown; this is not a successful benchmark result. See [provider trial record](PROVIDER_TRIALS.md).
- The checked-in fixture ablation remains a development artifact with no successful measured trials and must not be used for performance claims.

## Demo and cost

The portfolio page in `demo/index.html` is read-only and does not run submitted code. It can be published through GitHub Pages at no hosting cost for a public repository on GitHub Free. Publishing requires restoring this checkout's Git link to the supplied repository, pushing changes, and selecting GitHub Actions as the Pages source in repository settings.

Local tests and manifest checks need no API key. A real model call needs a provider key and is subject to that provider's current limits and billing terms. Public interactive code execution is intentionally not available until authentication, quotas, worker separation, persistence, and stronger isolation are built and tested.
