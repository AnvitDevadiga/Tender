# Implementation Plan

This plan preserves the existing Python evaluation loop and upgrades it in reviewable slices. Each slice should land with deterministic tests and truthful documentation. The full product brief is not implemented by the current baseline.

## Progress snapshot — 2026-10-10

- Phase 0 audit and contract: complete for the prototype. Architecture, security, workflow, benchmark, evaluation, and deployment docs are present.
- Phase 1 containment: local safeguards implemented: configured workspace-root containment, Docker-only API workflow mode, bounded disposable copies, link/special-file rejection, Git config scrubbing, constrained GitHub issue import, and container timeout cleanup. Public authentication, quotas, and stronger daemon-level isolation remain open.
- Phase 2 benchmark contract: schema v2 validates stable task IDs, full commit pins, setup/test/verification commands, clean sources, exact checkout identity, and time budgets. Remote repository materialization and immutable evaluator images remain open.
- Phase 3 workflow lifecycle: provider-generated plans with bounded schema/path validation, deterministic transitions through tests, static analysis, patch review, and clean-copy task verification; clean Git base pinning, patch/test/retry/artifact path, per-attempt Docker lifecycle, output caps, timeout cleanup, and fail-without-replay startup recovery implemented. A network-disabled Docker sandbox smoke test passed. Plan approval, durable worker resume, and a successful provider-backed code-generation run remain open.
- Phase 4 repository understanding and tools: bounded lexical ranking, capped Python AST symbol-definition matches, selection rationale, repository maps, and untrusted-content framing implemented. The provider now has a bounded structured tool loop with typed file, search, Git, dependency, test, lint, and sandboxed command tools, including path/secret/evaluator protections and redacted call auditing. Dependency-graph expansion and symbol indexing for other languages remain open.
- Phase 5 control plane: API run creation atomically persists a queued run and audit event, then returns. A separate worker atomically claims the oldest queued run, rechecks its relative workspace under the configured root, renews a lease, and executes it. SQLite schema migration, concurrent claims, expiry recovery without replay, and stale-owner write fencing are implemented. Local Compose separates the API from the Docker-socket worker and shares state/artifacts/workspaces. PostgreSQL/migrations, workflow checkpoint resume, cancellation, general API authentication, quotas, role-based approvals, and plan approvals remain open. A local dashboard exists; this is not a full product UI.
- Phase 6 deployment/research: the source is pushed to the supplied public GitHub repository, CI passes, and the interactive recorded-run portfolio demo is live through GitHub Pages. An 18-run exploratory Groq experiment now covers three unique tasks at retry budgets 0/1/2; broader independent task coverage and configuration comparisons remain open. Final name/UI guidance and public code execution remain open.
- Public project name: Tender. Internal Python package and repository names remain `forgebench` / `ForgeBench` for now.

## Phase 0 — audit and contract

- Record current behavior, security boundaries, tests, and deployment gaps.
- Define benchmark task schema v2 and immutable run identity.
- Specify state transitions, sandbox interface, event schema, and acceptance criteria.

## Phase 1 — contain untrusted execution

- Remove client control of arbitrary host paths; use server-managed workspace IDs under a configured root.
- Restrict public execution to curated tasks/repositories; add authentication, rate limits, run quotas, and request size limits before public deployment.
- Make Docker execution the only mode for untrusted tasks; default network off, minimal environment, non-root user, writable isolated worktree, explicit disk/time/CPU/memory/PID limits.
- Validate GitHub URLs against supported host/path formats and prevent redirects/SSRF.
- Add adversarial tests for path traversal, symlink escape, malicious issue/repository instructions, and environment leakage.

## Phase 2 — reproducible benchmark contract

- Version task schema with repository URL, full base commit, setup commands, target tests, evaluator-owned verification tests, timeout/resource budget, and dataset/task hashes.
- Validate immutable base commits and fail closed when pins are missing or workspace state is dirty.
- Preserve independent evaluator files outside agent-write scope; record setup and verification outputs.
- Enforce manifest wall-clock/token/retry budgets and record provider/configuration identity.
- Generate resolution, test, regression, retry, duration, tool, token, cost, patch-size, and failure metrics from raw records.

## Phase 3 — sandbox and workflow lifecycle

- Add `SandboxProvider` lifecycle contract and local trusted-fixture and Docker implementations.
- Move repository clone, patch application, commands, test output, diff capture, and cleanup into the sandbox.
- Implement explicit persisted state machine with bounded retries and repeated-failure detection.
- Use idempotency keys and durable transitions for commands with side effects; add resume/recovery tests.
- Add independent verifier for test integrity, target tests, static checks, and diff scope.

## Phase 4 — repository intelligence and tools

- Generate bounded repository maps with languages, entry points, configs, dependency manifests, build/test commands, and test locations.
- Upgrade lexical retrieval to deterministic ranked search; add supported AST/symbol lookup and dependency-aware expansion.
- Record why each file/range was selected and enforce retrieval budgets.
- Define typed tool schemas and central validation for file reads/writes, searches, commands, Git inspection, lint, and type checks.
- Capture every call's validated input, bounded output, duration, error, and exit status.

## Phase 5 — durable control and product UI

- Move from SQLite prototype to PostgreSQL with SQLAlchemy/Alembic migrations.
- Replace the local SQLite queue with PostgreSQL-backed jobs, renewable worker leases, coordinated recovery, and horizontal workers.
- Add authentication, repository/task permissions, quotas, approval records, and audit retention.
- Stabilize API contracts, then build the Next.js task, run, repository, benchmark, compare, evaluation, and settings routes.
- Add explicit human approval before pushes/PRs and protected branch changes.

## Phase 6 — deploy, CI, and measured research

- Split Compose services into web/API, PostgreSQL, queue, and workers; persist state and separate execution-plane credentials.
- Add production Dockerfiles, `.env.example`, health checks, resource limits, migrations, backup/restore and deployment docs.
- Add CI for formatting, lint, types where configured, unit/integration/security tests, and offline benchmark contract checks.
- Run pinned datasets across multiple configurations with repeated trials; publish raw artifacts, uncertainty/sample size, command, manifest/dataset hashes, and actual results.
- Add a curated demo with strict quotas only after abuse and isolation tests pass.

The recorded-run interactive portfolio deliverable is live at [anvitdevadiga.github.io/ForgeBench](https://anvitdevadiga.github.io/ForgeBench/). It demonstrates the project and links to source without implying that a public code-execution service exists.

## Definition of done gates

1. No untrusted generated code executes on the API host.
2. A run can resume after worker interruption without duplicate external side effects.
3. Benchmark tasks are pinned, evaluator integrity is enforced, and metrics derive from stored evidence.
4. Independent verification and trace inspection work end to end.
5. Public execution is authenticated or curated and quota-limited.
6. CI and deployment instructions match a clean deployment.
7. README claims include measured run provenance and limitations.
