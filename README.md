# ForgeBench (working name)

![ForgeBench](assets/forgebench-wordmark.svg)

ForgeBench is the current working name for an evaluation-first platform for autonomous software engineering agents. The final project name will be chosen later. It is designed to answer experimental questions about agent architecture, retrieval, context, retries, cost, and failure modes—not merely to expose a coding-agent demo.

## Current status

This repository contains a local evaluation prototype. It is not yet the complete autonomous engineering and public benchmarking platform described in the roadmap:

- FastAPI health, run creation, and run lookup endpoints.
- Typed issue, benchmark configuration, run, status, and failure-category models.
- A provider protocol that can later support OpenAI, Anthropic, Gemini, Groq, Ollama, or another backend.
- An offline deterministic provider and explicit failure behavior when execution is not configured.
- SQLite persistence for runs and append-only audit events.
- An evaluation contract that defines benchmark identity, outcomes, integrity rules, and metric provenance.
- A versioned experiment manifest for the first retry ablation.
- Repository snapshotting at an immutable Git commit.
- Deterministic local text retrieval.
- Bounded command execution and a test grader boundary.
- Docker execution with no network, CPU/memory/PID limits, and a repository-only mount.
- Patch artifacts with SHA-256 identity and protected evaluator-file checks.
- GitHub issue URL parsing and an optional authenticated GitHub API client.
- Groq Chat Completions provider via `GROQ_API_KEY` and `GROQ_MODEL`.
- End-to-end workflow seams for retrieval, unified-diff application, retries, and grading.
- Provider-generated plans are schema-validated and persisted; successful runs pass tests, built-in diff checks, optional static-analysis commands, patch-scope review, and exact-patch verification on a clean copy.
- Groq tool calls can use bounded typed repository tools for file edits, lexical search, Git inspection, dependency lookup, tests/lint, and sandboxed commands; sensitive paths and benchmark evaluators are protected.
- API runs can opt into a final approval state, resolved through an endpoint requiring a server-side bearer token; approval decisions are atomically audited.
- Detailed per-attempt audit events for retrieval, provider calls, patch application, grading, retries, and errors.
- Selectable local or Docker sandbox execution with bounded time, CPU, memory, PID, capability, and network controls.
- API workflow runs require a configured workspace root and Docker mode; a clean standalone Git source is pinned and copied to disposable agent and verifier workspaces before patching.
- Benchmark schema v2 requires stable task IDs, pinned commits, setup/test/verification commands, and task timeouts; optional static-analysis commands run as a separate gate. The runner enforces exact commit identity and manifest wall-clock limits.
- Bounded lexical retrieval with capped Python AST symbol matching, selection rationale, compact repository maps, and untrusted repository-content handling.
- GitHub Actions CI for Ruff lint, compilation, offline tests, and benchmark validation.
- Production-minded SQLite persistence with WAL mode, busy timeouts, and in-memory test support.
- `/metrics` and per-run artifact inspection endpoints for operational reporting.
- Benchmark validation with manifest checks, retry-budget breakdowns, failure categories, token/tool totals, cost, and latency.
- Experiment manifest loading, aggregate metrics, and failure counts.
- Runtime provider selection and a credential-safe `python -m forgebench check-groq` command.
- JSONL benchmark tasks and `run-experiment` execution with machine-readable result output.
- A local-first run explorer dashboard at `dashboard/index.html`, with a field-guide visual system, motion-led workflow cards, real run search, audit drawers, and honest empty states.
- A reusable brand asset set in `assets/`, while the dashboard itself uses a plain ForgeBench wordmark for a cleaner product surface.
- The supplied Canva wordmark, tightly cropped and preserved as `assets/forgebench-canva-cropped.png`.
- Docker packaging and tests for the API and persistence layer.

No benchmark metrics are included yet. Numbers must come from recorded experiments.

## Architecture seam

The intended pipeline is:

`Issue ingestion → repository index → retrieval/context → planner → sandbox workspace → patch/tool runner → grader → failure analysis → retry → report`

The API persists run requests to a SQLite-backed queue and returns immediately. A separate worker claims and executes queued work; PostgreSQL, migrations, durable leases, and worker health management remain future steps.

## Run locally

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python scripts/prepare_fixtures.py
uvicorn forgebench.api:app --reload
pytest
```

For a plain-language explanation of the project, free local use, API keys, and hosting limits, see [docs/FREE_SETUP.md](docs/FREE_SETUP.md). A static recruiter-facing overview is available in [`demo/index.html`](demo/index.html).

Copy [`.env.example`](.env.example) to `.env`, edit the local values, then export them into your shell with `set -a; source .env; set +a`. Keep `.env` private; it is ignored by Git.

## Free static portfolio demo

The read-only page is live at [anvitdevadiga.github.io/ForgeBench](https://anvitdevadiga.github.io/ForgeBench/). It does not expose the API or execute submitted code. The [`Deploy portfolio demo` workflow](.github/workflows/deploy-demo.yml) republishes it when `demo/` changes. GitHub Pages is available for public repositories on GitHub Free; see [GitHub's Pages documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site).

Or run the API with Docker Compose:

```bash
docker compose up --build
```

## API example

```bash
curl http://localhost:8000/health
```

For an actual patch-and-test workflow, configure a workspace root and use the Docker-only request in [Local API workflow](#local-api-workflow). A request without a workspace does not execute tests.

To use Groq, set `GROQ_API_KEY` in the worker environment (or in `.env` for Compose). The current development model is `openai/gpt-oss-20b`; use a configuration with `model: "groq/openai/gpt-oss-20b"`. Validate the connection without printing the key:

```bash
python -m forgebench check-groq
```

API workflow requests are disabled unless `FORGEBENCH_WORKSPACE_ROOT` is configured, and require `"sandbox":"docker"`. The API validates and persists the workspace then returns a `queued` run; the separate worker revalidates the workspace, creates a disposable copy, executes configured commands in Docker, and records the patch and trace as artifacts. `GET /runs/{run_id}` can be polled for progress. This local queue is not a replacement for authentication or public-demo quotas; do not expose the API publicly.

To require a final human decision for an API run, configure a private `FORGEBENCH_APPROVAL_TOKEN` (at least 32 characters) and set `config.require_human_approval` to `true`. The run remains `awaiting_approval` until an operator submits `POST /runs/{run_id}/approval` with `{"decision":"approve"}` or `{"decision":"reject"}` and `Authorization: Bearer <token>`. The approval token only protects that endpoint; the API still lacks general authentication and must stay local.

Start the API and open `/dashboard` to inspect persisted runs:

```bash
python -m uvicorn forgebench.api:app --reload
open http://127.0.0.1:8000/dashboard
```

Run an experiment from a JSONL task dataset:

```bash
python -m forgebench run-experiment \
  --manifest experiments/retry-ablation-v1.toml \
  --dataset benchmarks/tasks.jsonl \
  --output results/retry-ablation-v1.json
```

This command makes provider calls for the configured 27 trial runs and requires the configured model credentials. Run benchmark validation first; no model key is needed for validation.

Compare completed experiment files only when they share the same dataset hash:

```bash
python -m forgebench compare \
  --results results/model-a.json results/model-b.json \
  --output results/comparison.json
```

The comparison groups runs by model, architecture, and retrieval settings, and labels small task sets exploratory.

Each task must provide an issue, a clean local Git checkout, and evaluator commands. The runner copies the checkout for every repetition, records artifacts, and writes aggregate results. Replace the example workspace and commit before using it for benchmark claims.

## Project status

The repository contains a local research loop with pinned fixture tasks, provider abstraction, retrieval, patch application, selectable command runners, bounded retries, grading, persistent traces, artifacts, experiment manifests, aggregation, metrics endpoints, and a run explorer. The API/worker queue is persisted in SQLite with atomic claims, renewable leases, and stale-owner write fencing; expired jobs fail without automatic replay. Workflow checkpoint resume, PostgreSQL, public demo controls, production deployment, and broad benchmark evidence remain incomplete. Do not treat the local fixture as evidence of general coding-agent performance.

## Local API workflow

The API workflow accepts only workspaces beneath `FORGEBENCH_WORKSPACE_ROOT` and requires a working Docker daemon. For the checked-in fixture, start the API with the fixture directory as its allowed root:

```bash
export FORGEBENCH_WORKSPACE_ROOT="$PWD/benchmarks/fixtures"
uvicorn forgebench.api:app --reload
```

In a second terminal, run a request with `workspace` relative to that root:

```bash
curl -X POST http://localhost:8000/runs \
  -H 'content-type: application/json' \
  -d '{"benchmark_id":"local-check","workspace":"forgecalc","issue":{"repository":"local/forgecalc","title":"Fix percentage units","body":"15 should mean fifteen percent"},"config":{"model":"groq/openai/gpt-oss-20b","commands":["python3 -m pytest -q"],"sandbox":"docker","max_retries":1}}'
```

The `workspace` path is resolved under the configured root. The POST returns a queued run; poll `GET /runs/{run_id}` for its status. Start the worker with `python -m forgebench worker`, sharing `FORGEBENCH_DB_PATH`, `FORGEBENCH_WORKSPACE_ROOT`, and `FORGEBENCH_ARTIFACT_ROOT`. For a real model call, configure `GROQ_API_KEY` in the worker environment. This API is for local use; it does not yet implement public authentication or quotas.

The backend also exposes:

- `GET /metrics` — queued/running/completed counts, pass rate, latency, cost, token, tool-call, and failure totals.
- `POST /issues/github` — import a real GitHub issue by URL; set `GITHUB_TOKEN` for private or rate-limited access.
- `GET /runs/{run_id}/events` — append-only audit timeline.
- `GET /runs/{run_id}/artifact` — portable artifact inventory.
- `python -m forgebench validate-benchmark --manifest ... --dataset ...` — fail-fast dataset validation.

Use `"sandbox":"docker"` for network-disabled container execution when Docker is available. Docker is a stronger boundary than local execution, but this prototype is not yet a hardened public code execution service.

## Brand assets

- `assets/forgebench-mark.svg` — compact app icon and avatar.
- `assets/forgebench-wordmark.svg` — README, docs, and product header logo.
- `assets/forgebench-social-preview.svg` — GitHub repository social preview artwork.
- `assets/favicon.svg` — browser favicon.

## Roadmap

1. Add planner/executor and single-agent baselines.
2. Add OpenTelemetry export behind an optional dependency.
3. Add GitHub PR creation after explicit user approval.
4. Grow the permissively licensed benchmark beyond the local fixture.
5. Publish only after recorded experiments produce reproducible results.
