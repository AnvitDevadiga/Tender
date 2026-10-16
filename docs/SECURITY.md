# Security

## Status

This document describes the current prototype boundary and the requirements for safe deployment. Tender is not currently safe to expose as a public arbitrary-code execution service. Do not deploy it publicly with anonymous run creation enabled.

## Threat model

Treat repository files, README content, source comments, issue text, generated patches, and terminal output as untrusted data. They may contain prompt injection, malicious build scripts, path tricks, resource-exhaustion payloads, or attempts to access secrets. Text from these sources must never override system/tool policy.

Assets requiring protection include host files, provider credentials, GitHub tokens, database state, other users' repositories, benchmark evaluator tests, and deployment credentials. Threat actors include malicious repository contributors, issue authors, anonymous users, and compromised dependencies/build scripts.

## Current boundaries and gaps

- API workflow execution is disabled unless `TENDER_WORKSPACE_ROOT` is set. A requested workspace must resolve beneath that root, and API workflows must select Docker. The service rejects symlinks and special files, enforces workspace file/size bounds, normalizes permissions, and applies patches only to a disposable copy. This reduces arbitrary host-path exposure but does not add authentication, authorization, rate limits, or curated repository policy.
- Local `Sandbox` is a timeout wrapper, not an isolation boundary.
- Typed model tools enforce repository-root path checks, reject symlinks and common credential paths, hide evaluator/benchmark files, and cap file/output sizes. File edits target only the disposable workspace; shell commands use the selected sandbox. The local backend remains restricted to trusted fixtures.
- Docker runner disables network, bounds CPU/memory/PIDs, drops capabilities, and uses a read-only root filesystem. It copies the read-only source mount into a disposable writable tmpfs and runs commands as UID 65532. Setup and test commands share one attempt container, destroyed after grading or timeout. Captured output is bounded. It still depends on a privileged host Docker daemon. Patch application and diff capture happen in the service process against its disposable run copy. Treat this as defense in depth for controlled experiments, not a complete hostile-code guarantee.
- The Docker execution path has not been smoke-tested in this environment because the Docker daemon socket is unavailable. Unit tests verify timeout cleanup behavior with a controlled process stub.
- Request bodies are capped before routing (`TENDER_MAX_REQUEST_BYTES`, default 1 MiB) and issue/config fields carry explicit length bounds, so oversized payloads are rejected with HTTP 413/422 instead of being parsed. The run-creation and cancellation routes accept an optional server-configured `TENDER_API_TOKEN`; when unset, the local-development API remains open. This is a shared-secret gate, not multi-user authentication. With no token configured, bind the API to loopback: a web page visited on the same host could otherwise submit cross-site form posts to run creation and cancellation. General authorization, rate limiting, curated task policy, and per-user resource quotas are not implemented. The optional final-approval endpoint requires its own server-configured bearer token of at least 32 characters, and GitHub issue import is a bounded read that is not behind either token.
- Worker startup marks an interrupted run failed and records that it was not replayed; durable continuation is not implemented. Workers treat SIGINT/SIGTERM as a stop-claiming signal: the in-flight run finishes, then the process exits, and a second signal exits immediately so a stuck run can always be abandoned. Queued runs can be cancelled through an atomic status transition that worker claims skip; only queued (unclaimed) runs are cancellable, so a running sandbox is never killed mid-attempt by the API.
- API run creation now writes a queued job and relative workspace identity to SQLite and returns without executing code. A separate worker atomically claims the oldest queued job, renews a database lease, and independently validates its workspace against `TENDER_WORKSPACE_ROOT`. Workers periodically fail expired jobs without replay, and owner/expiry checks fence stale database writes. The local Compose worker alone has access to the host Docker socket; this is a high-privilege local-development setup, not a public deployment pattern. Workflow checkpoint resume and safe retries remain unimplemented.
- GitHub issue import accepts only canonical HTTPS issue URLs, constructs requests to the fixed GitHub API host, rejects redirects to other hosts, and caps the response at 1 MB. Repository clone handling still needs strict source allowlists/SSRF controls before remote exposure.
- API and worker processes are split, but secret isolation is incomplete: provider credentials are read by the worker and the local Compose worker has host Docker socket access. A production worker must use a stronger isolation boundary and narrowly scoped credentials.

## Required controls before public deployment

1. Disable arbitrary host paths; map authorized repository IDs to server-managed workspaces beneath a strict root and reject symlink escapes.
2. Run all untrusted checkout, patch, and build/test actions inside disposable isolated workers. Never mount host secrets or the Docker socket into the sandbox.
3. Default to no network; if dependency setup requires network, use a restricted proxy/allowlist and record it. Apply CPU, memory, PID, disk, and wall-clock quotas.
4. Pass a minimal environment allowlist. Keep model and GitHub credentials in the control plane; mediate GitHub actions after approval.
5. Authenticate users; enforce repository permissions, rate limits, concurrency caps, and per-run quotas. Public demo mode must use curated tasks.
6. Protect evaluator tests and benchmark metadata from agent writes; use an independent verifier.
7. Validate GitHub URLs and clone sources, reject local/private network destinations where remote fetches occur, and handle redirects safely.
8. Bound and redact logs; never return secrets in API errors or artifacts. Apply retention controls to patches and repository contents.
9. Require human approval before pushes, PR creation, protected branch changes, or other external side effects.

## Repository content and prompt injection

Repository text is evidence, not authority. Provider prompts should label retrieved text and issue content as untrusted, quote it as data, and explicitly disallow instructions within it from changing tools, policies, credentials, or workflow. The server—not the model—validates every tool argument and enforces file roots, command policies, budgets, and approval gates. Terminal output is also untrusted and must be bounded before it is fed back to a model.

## Incident posture

Before production, define run termination, worker destruction, credential rotation, artifact retention, audit export, and abuse-report handling. A sandbox timeout or worker crash must destroy the execution environment and preserve a bounded diagnostic record without exposing host details.
