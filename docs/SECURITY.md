# Security

## Status

Tender is a local, single-operator evaluation tool. It is **not** safe to expose
as a public arbitrary-code execution service, and it is not a hostile-code
sandbox you can point at untrusted repositories from the internet. Remote
repository sources are disabled.

## Threat model

Treat repository files (including `.git` metadata), README content, source
comments, issue text, generated or imported patches, and terminal output as
untrusted data. They may contain prompt injection, malicious build scripts, Git
helper configuration, path tricks, resource-exhaustion payloads, or attempts to
access secrets. Text from these sources never changes tool policy.

Assets to protect: host files and processes, provider credentials, GitHub
tokens, the run database, other repositories, evaluator tests and verification
configuration, and evidence integrity.

## Trust boundaries

| Zone | Trusted? | What runs there |
| --- | --- | --- |
| Tender process (API, worker, CLI) | Trusted code, untrusted inputs | Git plumbing against **sanitized metadata copies**, patch validation, evidence writing. Never repository code. |
| Source repository you select | Untrusted, **read-only** | Nothing. Tender reads files and copies `.git` bytes; it never runs Git against the original metadata. |
| Agent workspace (temporary) | Untrusted | Agent tool edits. Commands run only through the selected sandbox backend. |
| Verifier workspaces (temporary, one per check) | Untrusted after patch application | Candidate patch + evaluator-owned files; checks run through the sandbox backend. |
| Docker sandbox | Isolation boundary (defense in depth) | Setup, target, regression, and static-analysis commands. |
| Local backend | **No isolation** | A timeout wrapper on the host. Use only for code you wrote or fully trust. |

## Controls in this version

### Repository boundary (`src/tender/snapshot.py`)

- A repository's local Git config can make Git execute helpers (`core.fsmonitor`,
  filter drivers, `diff.external`, hooks, `include.path`). Tender never runs Git
  against the source's own `.git`. It copies the metadata as plain bytes into a
  private temporary directory (rejecting symlinks, special files, linked
  worktrees, object alternates, and non-SHA-1 repositories), replaces the copied
  config with a minimal Tender-written one, drops hooks, and runs every Git
  command with global/system config disabled and helper settings overridden.
- Working-tree status is computed by pointing the sanitized copy at the source
  tree; any index refresh writes to the private copy.
- Agent and verifier workspaces are materialized from Git objects at an exact
  commit. Ignored files (for example `.env`) and uncommitted edits never reach a
  workspace. Trees containing symlinks or submodules are rejected.
- Earlier versions ran `git status`/`git rev-parse` directly on the source and
  `git checkout` inside a raw filesystem copy, so a planted `core.fsmonitor`
  helper or `post-checkout` hook could run on the host. See
  [Evaluation errata](EVALUATION_ERRATA.md).

### Commit identity

- API/worker runs resolve the repository HEAD through the sanitized copy and
  reject the run **before any provider call** when it disagrees with
  `issue.base_commit`, when the source is dirty, or (with
  `TENDER_REQUIRE_PINNED_COMMIT=1`) when no commit was requested. Requested and
  executed commits are both recorded.

### Pristine verification

- Every verification check (target, then regression) runs in a brand-new
  workspace materialized at the validated base commit; the cumulative candidate
  patch is applied there and the directory is deleted afterwards. The workspace
  fingerprint (HEAD, index digest, cleanliness) is recorded before the patch.

### Evaluator integrity (`src/tender/integrity.py`)

- `protected_paths` (default `.tender/**`), evaluator assets, `.git`
  components, and modules that would shadow the test tooling (`pytest.py`,
  `_pytest`, `sitecustomize.py`, `usercustomize.py`, `*.pth`, …) are integrity
  violations: the candidate is rejected and never verified.
- `restored_paths` (defaults: `tests/**`, `test/**`, `conftest.py`,
  `**/conftest.py`, `pytest.ini`, `pytest.toml`, `.pytest.toml`, `noxfile.py`)
  are pure evaluator files: they are reset to base-commit content in every
  verifier, and files the patch added there are removed.
- Mixed-purpose configuration files are not restored whole. Only their
  test-runner section (`[tool.pytest*]` in `pyproject.toml`, `[tool:pytest]` in
  `setup.cfg`, `[pytest]` in `tox.ini`) is reset; application and build
  settings are tested exactly as submitted. The result is re-parsed and must
  match the base section and the candidate's other values exactly. Otherwise, for
  example with a non-standard form or a deleted file that held evaluator
  settings, the run fails closed as an integrity violation and is not reported
  as independently verified. Reports state whether the tested contents equal
  the submitted patch (`tested_as_submitted`).
- Hidden evaluator assets are read before any candidate runs, are never
  visible in the agent workspace, are written into verifiers after the patch,
  and are recorded by digest. Their output is withheld from agent feedback.
- Verification commands, criteria, and result computation live in the Tender
  process; exit codes are read by the host, not written by the candidate.

### Tool authorization

- The allowlist is enforced at dispatch, not only when advertising tools. Calls
  to unknown or unauthorized tools are refused before argument parsing, recorded
  as `agent_tool.rejected` events, and three refusals end the attempt as a
  `policy_rejection`. `run_command` is never granted implicitly.

### Network

- Credentialed HTTP (GitHub, model providers) uses an opener that refuses
  redirects, because `urllib` forwards `Authorization` headers to redirect
  targets. Provider endpoints must use HTTPS unless they are loopback servers.

### Docker backend

- Network disabled, CPU/memory/PID limits, all capabilities dropped,
  `no-new-privileges`, read-only root filesystem, UID 65532, source mounted
  read-only and copied to a tmpfs. One container per grading group, removed on
  completion, timeout, or cancellation.

### API

- Request bodies are capped (`TENDER_MAX_REQUEST_BYTES`), optional bearer token
  for mutating routes (`TENDER_API_TOKEN`), optional rate limiting, strict
  dashboard CSP, `nosniff`, `DENY` framing, `no-referrer`.

## Known limitations (not fixed)

- **The code under test runs inside the same process as the test runner.** A
  patch designed to forge results (for example by exiting with status 0 at
  import time, or by locating and rewriting a report file) can still mislead a
  command-exit-code check. Restoring evaluator files and rejecting tool shadowing
  removes the common vectors. Structured pytest checks in patch evaluation also
  take their required test IDs from candidate-independent sources and reconcile
  them with a total-checked JUnit report: every required test, identified by its
  full node ID, must be reported exactly once as passed. A patch that deletes or
  rewrites report entries is caught. What remains possible is code that
  rewrites the report consistently *and* forces exit status 0 from inside the
  test process.
  Treat acceptance as strong evidence, not proof.
- Docker isolation depends on a privileged host Docker daemon. The local Compose
  worker mounts the Docker socket, which is equivalent to host root.
- The local backend is not isolation. It exists for trusted fixtures and
  explicitly trusted patches (`--trust-local-execution`).
- Container image provenance is not pinned by digest unless you configure
  `TENDER_SANDBOX_IMAGE` with one.
- Docker `create`/`start`/initialization calls have fixed 30-second limits and
  are not interrupted by cancellation; command execution is.
- The API has a shared-secret gate, not user authentication or authorization.
  Without `TENDER_API_TOKEN`, bind it to loopback.
- Evidence bundles are hashed and self-consistent, but not signed or
  independently attested.

## Required controls before any public deployment

1. Map authorized repository IDs to server-managed workspaces; never accept
   arbitrary host paths.
2. Run all candidate execution in disposable isolated workers without the Docker
   socket or host secrets.
3. Default to no network; if dependency setup needs network, use an allowlisted
   proxy and record it.
4. Authenticate users; enforce repository permissions, quotas, and concurrency.
5. Keep credentials in the control plane; require human approval for any
   external side effect.
6. Sign evidence bundles with a key the evaluator controls.

## Repository content and prompt injection

Repository text is evidence, not authority. Prompts label retrieved text and
issue content as untrusted data. The server, not the model, validates every tool
argument and enforces file roots, the tool allowlist, budgets, and approval
gates. Terminal output is bounded before it is fed back to a model.
