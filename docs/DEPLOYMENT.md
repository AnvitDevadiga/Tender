# Deployment

## Current local deployment

The current `docker-compose.yml` builds separate API, worker, and sandbox images. The API image has no Docker CLI or socket. Both services share a host-persisted SQLite database, artifact directory, and curated fixture workspace. The worker alone receives the Docker socket and uses a host-visible temp bind mount so the host Docker daemon can access sandbox source paths. The dedicated sandbox image includes Git for repository tools and is used inside resource-limited attempt containers. This setup is for local development only. Docker socket access grants powerful control over the host and this is not a safe public arbitrary-code execution deployment.
The API port binds to `127.0.0.1` so it is not exposed on the local network by default.

```bash
docker compose up --build
```

Compose mounts local state under `.tender-data/`, which is gitignored. The example fixture workspace is mounted read-only at `/workspaces`; use task paths relative to it. The worker needs the host Docker daemon running. Workers heartbeat renewable leases; expired jobs are marked failed without replay, and stale owners cannot commit further run state. The queue can coordinate concurrent claims, though this local Compose example starts one worker by default.

Copy `.env.example` to `.env`; Compose interpolates the listed provider, approval, and GitHub values into only the required service. Never commit `.env`. Never mount the Docker socket into a public API container or a public worker.

## API operations

- `GET /health` is a liveness check; `GET /ready` verifies the SQLite store answers queries and is suitable for container readiness probes.
- `GET /runs` supports `status`, `limit`, `offset`, and `summary=true` (drops patch, test-output, and issue-body payloads); `GET /runs/{id}/events` supports `limit` and `offset` and returns up to 5000 events per page by default (20000 maximum), so page long histories with `offset`; `GET /metrics` reports queue depth, pass rate, latency percentiles (p50/p95), token/tool totals, and per-model/per-benchmark summaries.
- `POST /runs` enqueues work. `POST /runs/{id}/cancel` cancels a queued (unclaimed) run atomically; running runs cannot be cancelled and return HTTP 409.
- Set `TENDER_API_TOKEN` to require `Authorization: Bearer <token>` on `POST /runs` and `POST /runs/{id}/cancel`; leave it unset only while the API stays bound to loopback. Set `TENDER_MAX_REQUEST_BYTES` to tune the request-body cap.
- Workers finish the current run and exit on SIGINT/SIGTERM; a second stop signal exits immediately if a run is stuck. `TENDER_WORKER_RECOVERY_SECONDS` controls how often idle workers scan for expired leases (default 15s).

## Production target

```mermaid
flowchart LR
  Browser --> Web[Next.js]
  Web --> API[FastAPI control plane]
  API --> DB[(PostgreSQL)]
  API --> Queue[(Durable queue)]
  Queue --> Worker[Execution worker]
  Worker --> Sandbox[Isolated sandbox]
  Sandbox --> Repo[Pinned repository + tests]
```

The control plane and execution plane should scale independently. Workers must not inherit provider, database, or GitHub credentials. Sandbox jobs should be disposable and resource-limited. Use authentication, per-user quotas, curated public demo tasks, and explicit approval for GitHub writes.

The repository does not yet provide production deployment manifests, PostgreSQL migrations, production worker health management, or a public coding execution service. The portfolio page is interactive playback of recorded evidence, but accepts no task and runs no submitted code. Local SQLite-backed worker leases are implemented, but they do not replace production queue/health infrastructure. Do not infer production readiness from the local Compose file. Serverless request execution is not suitable for persistent sandbox workloads.

## Pre-production checklist

- Configure PostgreSQL and migrations, worker leases/health checks, and persistent artifact storage.
- Separate API and worker identities and credentials; do not mount the Docker socket into the API.
- Enforce auth, permissions, rate/concurrency limits, and curated demo restrictions.
- Apply network, CPU, memory, PID, disk, and duration limits to disposable sandboxes.
- Configure TLS, secret management, log redaction/retention, backups, and recovery procedures.
- Run CI/security tests and verify a pinned end-to-end benchmark in the deployment environment.
