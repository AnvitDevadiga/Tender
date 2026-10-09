from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .models import ApprovalDecision, EvaluationRun, FailureCategory, RunEvent, RunStatus, utcnow
from .state import WorkflowState


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different request payload."""


class ApprovalConflict(ValueError):
    """A run is not awaiting a decision or already has an approval record."""


class LeaseLost(RuntimeError):
    """A worker no longer owns a live run lease and must stop committing changes."""


class RunStore:
    """Small SQLite persistence boundary; PostgreSQL can replace it without changing services."""

    def __init__(self, path: str | Path = "forgebench.db") -> None:
        self.path = str(path)
        self._memory = self.path == ":memory:"
        self._dsn = (
            f"file:forgebench-{id(self)}?mode=memory&cache=shared" if self._memory else self.path
        )
        self._anchor = sqlite3.connect(self._dsn, uri=self._memory) if self._memory else None
        if not self._memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._dsn, uri=self._memory)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        if not self._memory:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    benchmark_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    heartbeat_at TEXT
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    name TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run_id ON run_events(run_id);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                    request_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_approvals (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                    decision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for name in ("lease_owner", "lease_expires_at", "heartbeat_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_status_lease ON runs(status, lease_expires_at)"
            )

    def save_run(self, run: EvaluationRun, lease_owner: str | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease_owner is not None:
                self._assert_live_lease(db, str(run.id), lease_owner)
            self._save_run(db, run)

    def create_run_if_absent(
        self,
        run: EvaluationRun,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        created_event: RunEvent | None = None,
    ) -> tuple[EvaluationRun, bool]:
        """Persist a queued run once, atomically reserving its optional idempotency key."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = db.execute(
                    "SELECT r.payload, k.request_hash FROM idempotency_keys k "
                    "JOIN runs r ON r.id = k.run_id WHERE k.key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    if request_hash != existing["request_hash"]:
                        raise IdempotencyConflict(
                            "idempotency key was already used with a different request"
                        )
                    return EvaluationRun.model_validate_json(existing["payload"]), False
            existing_run = db.execute(
                "SELECT payload FROM runs WHERE id = ?", (str(run.id),)
            ).fetchone()
            if existing_run:
                return EvaluationRun.model_validate_json(existing_run["payload"]), False
            self._save_run(db, run)
            if created_event is not None:
                if created_event.run_id != run.id:
                    raise ValueError("run and created event IDs must match")
                db.execute(
                    "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                    (
                        str(created_event.run_id),
                        created_event.name,
                        created_event.timestamp.isoformat(),
                        json.dumps(created_event.payload),
                    ),
                )
            if idempotency_key:
                if not request_hash:
                    raise ValueError("request_hash is required with an idempotency key")
                db.execute(
                    "INSERT INTO idempotency_keys (key, run_id, request_hash) VALUES (?, ?, ?)",
                    (idempotency_key, str(run.id), request_hash),
                )
            return run, True

    def claim_run(self, run_id: str) -> EvaluationRun | None:
        """Atomically transition one queued run to running; only one caller can claim it."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM runs WHERE id = ? AND status = 'queued'", (run_id,)
            ).fetchone()
            if not row:
                return None
            run = EvaluationRun.model_validate_json(row["payload"])
            run.status = RunStatus.RUNNING
            db.execute(
                "UPDATE runs SET status = ?, payload = ? WHERE id = ? AND status = 'queued'",
                (run.status.value, run.model_dump_json(), run_id),
            )
            return run

    def claim_next_queued_run(
        self, worker_id: str = "worker", lease_seconds: int = 90
    ) -> EvaluationRun | None:
        """Atomically claim the oldest queued run and assign a renewable lease."""
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = utcnow()
        lease_expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, payload FROM runs WHERE status = 'queued' "
                "ORDER BY created_at ASC, id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run = EvaluationRun.model_validate_json(row["payload"])
            run.status = RunStatus.RUNNING
            changed = db.execute(
                "UPDATE runs SET status = ?, payload = ?, lease_owner = ?, "
                "lease_expires_at = ?, heartbeat_at = ? WHERE id = ? AND status = 'queued'",
                (
                    run.status.value,
                    run.model_dump_json(),
                    worker_id,
                    lease_expiry,
                    now.isoformat(),
                    row["id"],
                ),
            ).rowcount
            if changed != 1:
                return None
            db.execute(
                "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                (
                    str(run.id),
                    "worker.claimed",
                    now.isoformat(),
                    json.dumps(
                        {
                            "worker_id": worker_id,
                            "lease_expires_at": lease_expiry,
                        }
                    ),
                ),
            )
            return run

    def heartbeat_run(self, run_id: str, worker_id: str, lease_seconds: int = 90) -> bool:
        """Extend an active worker lease only when this worker still owns the run."""
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        now = utcnow()
        lease_expiry = now + timedelta(seconds=lease_seconds)
        with self._connect() as db:
            changed = db.execute(
                "UPDATE runs SET lease_expires_at = ?, heartbeat_at = ? "
                "WHERE id = ? AND status = 'running' AND lease_owner = ?",
                (lease_expiry.isoformat(), now.isoformat(), run_id, worker_id),
            ).rowcount
        return changed == 1

    @staticmethod
    def _assert_live_lease(db: sqlite3.Connection, run_id: str, worker_id: str) -> None:
        row = db.execute(
            "SELECT status, lease_owner, lease_expires_at FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != RunStatus.RUNNING.value
            or row["lease_owner"] != worker_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= utcnow().isoformat()
        ):
            raise LeaseLost(f"worker lease is no longer valid for run {run_id}")

    def resolve_approval(
        self,
        run_id: str,
        decision: ApprovalDecision,
        note: str,
        actor: str = "authenticated_operator",
    ) -> EvaluationRun:
        """Persist one authenticated final decision and its state transition atomically."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            run = EvaluationRun.model_validate_json(row["payload"])
            if not run.config.require_human_approval:
                raise ApprovalConflict("human approval was not required for this run")
            if run.status is not RunStatus.AWAITING_APPROVAL:
                raise ApprovalConflict("run is not awaiting human approval")
            previous = run.current_state
            if previous is not WorkflowState.HUMAN_FINAL_APPROVAL:
                raise ApprovalConflict("run state does not allow human approval")
            target = (
                WorkflowState.COMPLETE
                if decision is ApprovalDecision.APPROVE
                else WorkflowState.FAILED
            )
            run.status = (
                RunStatus.PASSED if decision is ApprovalDecision.APPROVE else RunStatus.FAILED
            )
            run.current_state = target
            if decision is ApprovalDecision.APPROVE:
                run.failure_category = FailureCategory.NONE
            else:
                run.failure_category = FailureCategory.UNKNOWN
                run.test_output = (run.test_output or "") + "\nRejected by human reviewer."
            timestamp = utcnow()
            approval_id = str(uuid4())
            db.execute(
                "INSERT INTO run_approvals "
                "(approval_id, run_id, decision, actor, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (approval_id, run_id, decision.value, actor, note, timestamp.isoformat()),
            )
            self._save_run(db, run)
            events = (
                RunEvent(
                    run_id=run.id,
                    name="approval.resolved",
                    timestamp=timestamp,
                    payload={
                        "approval_id": approval_id,
                        "decision": decision.value,
                        "actor": actor,
                        "note": note,
                    },
                ),
                RunEvent(
                    run_id=run.id,
                    name="workflow.state_changed",
                    timestamp=timestamp,
                    payload={"from": previous.value, "to": target.value, "summary": note[:500]},
                ),
                RunEvent(
                    run_id=run.id,
                    name="workflow.finished",
                    timestamp=timestamp,
                    payload={
                        "status": run.status.value,
                        "failure_category": run.failure_category.value,
                    },
                ),
            )
            db.executemany(
                "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                [
                    (
                        str(event.run_id),
                        event.name,
                        event.timestamp.isoformat(),
                        json.dumps(event.payload),
                    )
                    for event in events
                ],
            )
            return run

    def recover_interrupted_runs(self) -> list[EvaluationRun]:
        """Fail runs without a live worker lease; never replay interrupted side effects."""
        recovered: list[EvaluationRun] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = utcnow().isoformat()
            rows = db.execute(
                "SELECT id, payload, lease_owner, lease_expires_at FROM runs "
                "WHERE status = 'running' AND "
                "(lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (now,),
            ).fetchall()
            for row in rows:
                run = EvaluationRun.model_validate_json(row["payload"])
                interrupted_state = run.current_state.value if run.current_state else None
                run.status = RunStatus.FAILED
                run.failure_category = FailureCategory.ENVIRONMENT
                run.current_state = WorkflowState.FAILED
                run.test_output = (
                    "Run interrupted by service restart; it was not replayed automatically."
                )
                run.metadata["interrupted"] = True
                run.metadata["recovery_policy"] = "failed_without_replay"
                run.metadata["interrupted_at_state"] = interrupted_state
                run.metadata["interrupted_lease_owner"] = row["lease_owner"]
                run.metadata["interrupted_lease_expires_at"] = row["lease_expires_at"]
                self._save_run(db, run)
                timestamp = utcnow().isoformat()
                db.execute(
                    "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                    (
                        str(run.id),
                        "run.interrupted",
                        timestamp,
                        json.dumps(
                            {
                                "reason": "worker_lease_expired",
                                "replayed": False,
                                "interrupted_at_state": interrupted_state,
                                "lease_owner": row["lease_owner"],
                                "lease_expires_at": row["lease_expires_at"],
                            }
                        ),
                    ),
                )
                db.execute(
                    "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                    (
                        str(run.id),
                        "run.finished",
                        timestamp,
                        json.dumps(
                            {
                                "status": run.status.value,
                                "failure_category": run.failure_category.value,
                            }
                        ),
                    ),
                )
                recovered.append(run)
        return recovered

    @staticmethod
    def _save_run(db: sqlite3.Connection, run: EvaluationRun) -> None:
        db.execute(
            """
            INSERT INTO runs (id, status, benchmark_id, created_at, payload)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                benchmark_id = excluded.benchmark_id,
                created_at = excluded.created_at,
                payload = excluded.payload,
                lease_owner = CASE WHEN excluded.status = 'running'
                    THEN runs.lease_owner ELSE NULL END,
                lease_expires_at = CASE WHEN excluded.status = 'running'
                    THEN runs.lease_expires_at ELSE NULL END,
                heartbeat_at = CASE WHEN excluded.status = 'running'
                    THEN runs.heartbeat_at ELSE NULL END
            """,
            (
                str(run.id),
                run.status.value,
                run.benchmark_id,
                run.created_at.isoformat(),
                run.model_dump_json(),
            ),
        )

    def get_run(self, run_id: str) -> EvaluationRun | None:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id = ?", (run_id,)).fetchone()
        return EvaluationRun.model_validate_json(row["payload"]) if row else None

    def list_runs(self, limit: int = 100) -> list[EvaluationRun]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [EvaluationRun.model_validate_json(row["payload"]) for row in rows]

    def append_event(self, event: RunEvent) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                (
                    str(event.run_id),
                    event.name,
                    event.timestamp.isoformat(),
                    json.dumps(event.payload),
                ),
            )

    def save_run_with_event(
        self, run: EvaluationRun, event: RunEvent, lease_owner: str | None = None
    ) -> None:
        """Atomically persist a workflow state update and its audit event."""
        if event.run_id != run.id:
            raise ValueError("run and event IDs must match")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if lease_owner is not None:
                self._assert_live_lease(db, str(run.id), lease_owner)
            self._save_run(db, run)
            db.execute(
                "INSERT INTO run_events (run_id, name, timestamp, payload) VALUES (?, ?, ?, ?)",
                (
                    str(event.run_id),
                    event.name,
                    event.timestamp.isoformat(),
                    json.dumps(event.payload),
                ),
            )

    def get_events(self, run_id: str) -> list[RunEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT run_id, name, timestamp, payload FROM run_events "
                "WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            RunEvent(
                run_id=row["run_id"],
                name=row["name"],
                timestamp=row["timestamp"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]
