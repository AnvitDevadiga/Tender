# Deployment

Tender is a local, single-operator tool. These instructions run it on your own
machine. Do not expose the API publicly (see [Security](SECURITY.md)).

## Prerequisites

| Need | For | Notes |
| --- | --- | --- |
| Python 3.12+ and Git | everything | `pip install -e '.[dev]'` or the built wheel |
| Docker daemon + `tender-sandbox:local` | untrusted code (patch verification, API runs) | `docker build -f Dockerfile.sandbox -t tender-sandbox:local .` (includes pytest; no network at run time) |
| Fixture commits | benchmark tasks and tests | `python scripts/prepare_fixtures.py` (deterministic) |
| Model provider key | agent runs with a real model only | `GROQ_API_KEY`, or `OPENAI_API_KEY` (+ `OPENAI_BASE_URL`, HTTPS unless loopback) |
| Node.js | JavaScript unit tests only | optional |

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `TENDER_DB_PATH` | `tender.db` | SQLite database |
| `TENDER_WORKSPACE_ROOT` | unset (API execution disabled) | API/worker runs must name a workspace beneath this root |
| `TENDER_ARTIFACT_ROOT` | `artifacts` | Worker artifact directories |
| `TENDER_REPORT_ROOT` | unset | Directory of patch reports served at `/reports` |
| `TENDER_SANDBOX_IMAGE` | `python:3.12-slim` | Docker image for commands; use `tender-sandbox:local` (pin by digest for provenance) |
| `TENDER_REQUIRE_PINNED_COMMIT` | off | Reject API runs without `issue.base_commit` |
| `TENDER_API_TOKEN` | unset | Bearer token for mutating routes; bind to loopback without it |
| `TENDER_APPROVAL_TOKEN` | unset | ≥32 characters; enables the human-approval endpoint |
| `TENDER_MAX_REQUEST_BYTES` | 1048576 | Request body cap |
| `TENDER_RATE_LIMIT_PER_MINUTE`, `TENDER_RATE_LIMIT_BURST` | 0 (off), 10 | In-process limiter for mutating routes |
| `TENDER_WORKER_POLL_SECONDS`, `TENDER_WORKER_LEASE_SECONDS`, `TENDER_WORKER_RECOVERY_SECONDS` | 1, 90, 15 | Worker tuning |

## Local API and dashboard

```bash
TENDER_REPORT_ROOT=demo/data/patch-demo uvicorn tender.api:app --host 127.0.0.1
open http://127.0.0.1:8000/dashboard/
```

The dashboard is packaged inside the wheel (`tender/web/`), so installed
packages and Docker images serve it without a source checkout.

## Queue + worker

```bash
export TENDER_WORKSPACE_ROOT=$PWD/benchmarks/fixtures TENDER_SANDBOX_IMAGE=tender-sandbox:local
uvicorn tender.api:app --host 127.0.0.1 &
tender worker &
curl -s -X POST http://127.0.0.1:8000/runs -H 'content-type: application/json' -d '{
  "workspace": "forgecalc",
  "config": {"sandbox": "docker", "model": "groq/openai/gpt-oss-20b"},
  "issue": {"repository": "local/forgecalc", "title": "Fix percentage",
            "base_commit": "<full commit of the fixture HEAD>"}}'
```

The API only queues. The worker claims a run, holds a renewable lease, validates
the workspace and commit, and executes. Expired runs fail without replay. Only
queued runs can be cancelled.

## Docker Compose (local development)

```bash
cp .env.example .env    # fill only what you need; never commit it
docker compose up --build
```

Compose builds the API, worker, and sandbox images. The API binds to
`127.0.0.1:8000` and has no Docker access. **The worker mounts the host Docker
socket, which is equivalent to root on the host.** This is for local development
only. State is kept under `.tender-data/` (git-ignored); fixtures are mounted
read-only at `/workspaces`.

## Static demo (GitHub Pages)

```bash
python scripts/record_replay.py --check
bash scripts/build_demo_site.sh _site
python3 -m http.server 8765 --directory _site
```

`.github/workflows/deploy-demo.yml` publishes `_site` to
`https://anvitdevadiga.github.io/Tender/` on pushes to `main` that touch the
demo, dashboard, or published experiments. The site is static and read-only.

## API operations

- `GET /health` liveness; `GET /ready` checks the database.
- `GET /runs` (`status`, `limit`, `offset`, `summary=true`; `X-Total-Count`),
  `GET /runs/{id}`, `GET /runs/{id}/events`, `GET /runs/{id}/artifact`.
- `POST /runs`, `POST /runs/{id}/cancel`, `POST /runs/{id}/approval`.
- `GET /metrics`: SQL aggregates over every run, with `coverage`.
- `GET /reports`, `GET /reports/{id}`: patch verification reports.
