from __future__ import annotations

import os
import tempfile
from pathlib import Path
from time import perf_counter

from .artifacts import ArtifactStore
from .models import EvaluationRun, FailureCategory, RunEvent, RunStatus
from .outcomes import EvaluationOutcome
from .providers import DeterministicProvider, ModelProvider
from .repository import RepositoryError
from .snapshot import SourceIdentity, TrustedRepository
from .state import WorkflowState
from .store import LeaseLost, RunStore
from .workflow import AgentWorkflow

MAX_STORED_TEST_OUTPUT_BYTES = 256 * 1024
_OUTPUT_TRUNCATION_MARKER = (
    "\n...[run payload truncated; full gate output remains in run artifacts]"
)


def _bounded_output(text: str, limit: int = MAX_STORED_TEST_OUTPUT_BYTES) -> str:
    """Cap the copy of gate output stored in the run payload.

    Per-gate command output stays complete in the run artifact directory, so
    the database row only needs enough to explain the outcome.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + _OUTPUT_TRUNCATION_MARKER


class CommitPolicyError(RepositoryError):
    """The repository state does not match what the run is allowed to execute."""


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class RunService:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        store: RunStore | None = None,
        artifacts: ArtifactStore | None = None,
        *,
        require_pinned_commit: bool | None = None,
    ) -> None:
        self.provider = provider or DeterministicProvider()
        self.store = store or RunStore()
        self.artifacts = artifacts or ArtifactStore()
        self.runs: dict[str, EvaluationRun] = {}
        self.require_pinned_commit = (
            _env_flag("TENDER_REQUIRE_PINNED_COMMIT")
            if require_pinned_commit is None
            else require_pinned_commit
        )

    def validate_commit_identity(self, run: EvaluationRun, identity: SourceIdentity) -> str:
        """Return the commit to execute, or raise before any provider call is made."""
        executed = identity.head_commit
        if executed is None:
            raise CommitPolicyError("repository has no resolvable HEAD commit")
        if identity.dirty:
            raise CommitPolicyError(
                "workspace must be clean before a coding-agent run "
                f"({len(identity.status_entries)} uncommitted or untracked entries)"
            )
        requested = run.issue.base_commit.lower() if run.issue.base_commit else None
        if requested is None and self.require_pinned_commit:
            raise CommitPolicyError("issue.base_commit is required when commit pinning is enforced")
        if requested is not None and requested != executed:
            raise CommitPolicyError(
                f"requested base commit {requested} does not match repository HEAD {executed}"
            )
        return executed

    def _event(
        self, run: EvaluationRun, name: str, payload: dict[str, object] | None = None
    ) -> None:
        self.store.append_event(RunEvent(run_id=run.id, name=name, payload=payload or {}))

    def _workflow_event(
        self,
        run: EvaluationRun,
        name: str,
        payload: dict[str, object],
        lease_owner: str | None = None,
    ) -> None:
        if name == "workflow.state_changed":
            run.current_state = WorkflowState(str(payload["to"]))
            self.store.save_run_with_event(
                run,
                RunEvent(run_id=run.id, name=name, payload=payload),
                lease_owner=lease_owner,
            )
            return
        self._event(run, name, payload)

    async def execute_workflow(
        self,
        run: EvaluationRun,
        workspace: str,
        *,
        already_claimed: bool = False,
        lease_owner: str | None = None,
    ) -> EvaluationRun:
        started = perf_counter()
        if already_claimed:
            if run.status is not RunStatus.RUNNING:
                raise ValueError("already_claimed requires a running run")
        else:
            self.store.create_run_if_absent(run)
            claimed = self.store.claim_run(str(run.id))
            if claimed is None:
                return self.store.get_run(str(run.id)) or run
            run = claimed
        self._event(
            run,
            "workflow.started",
            {"workspace": "disposable-copy", "benchmark_id": run.benchmark_id},
        )
        workflow = AgentWorkflow(self.provider)
        try:
            with TrustedRepository(workspace) as repository:
                identity = repository.identity()
                run.metadata["requested_base_commit"] = run.issue.base_commit
                run.metadata["source_repository"] = {
                    "branch": identity.branch,
                    "head_commit": identity.head_commit,
                    "dirty": identity.dirty,
                    "status_digest": identity.status_digest,
                }
                try:
                    base_commit = self.validate_commit_identity(run, identity)
                except CommitPolicyError as exc:
                    return self._reject(run, str(exc), started, lease_owner)
                run.metadata["base_commit"] = base_commit
                run.metadata["executed_base_commit"] = base_commit
                with tempfile.TemporaryDirectory(prefix="tender-run-") as temp_dir:
                    isolated_workspace = repository.materialize(
                        base_commit, Path(temp_dir) / "workspace"
                    )
                    self._event(
                        run,
                        "workspace.isolated",
                        {
                            "copy": "created",
                            "snapshot": "git-objects-at-commit",
                            "source_modified": False,
                            "base_commit": base_commit,
                            "requested_base_commit": run.issue.base_commit,
                            "verifier": "fresh-workspace-per-check",
                        },
                    )
                    result = await workflow.run(
                        run.issue,
                        isolated_workspace,
                        run.config.commands,
                        run.config.max_retries,
                        sandbox_backend=run.config.sandbox.value,
                        timeout_seconds=run.config.timeout_seconds,
                        event_sink=lambda name, payload: self._workflow_event(
                            run, name, payload, lease_owner
                        ),
                        setup_commands=run.config.setup_commands,
                        static_analysis_commands=run.config.static_analysis_commands,
                        verification_commands=run.config.verification_commands,
                        verification_workspace=repository,
                        require_human_approval=run.config.require_human_approval,
                        retrieval_limit=run.config.retrieval_limit,
                        base_commit=base_commit,
                        regression_commands=run.config.regression_commands,
                        integrity=run.config.integrity,
                        deadline_seconds=run.config.deadline_seconds,
                    )
                try:
                    after = repository.identity()
                except RepositoryError:
                    run.metadata["source_unchanged"] = None  # could not be re-observed
                else:
                    run.metadata["source_unchanged"] = (
                        after.head_commit == identity.head_commit
                        and after.status_digest == identity.status_digest
                    )
        except LeaseLost:
            return self.store.get_run(str(run.id)) or run
        except RepositoryError as exc:
            # Unsafe or unsupported repository state (symlinks, linked worktrees,
            # missing commits): rejected before any provider call.
            return self._reject(run, str(exc), started, lease_owner)
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.failure_category = FailureCategory.TOOLING
            previous_state = run.current_state.value if run.current_state else None
            run.current_state = WorkflowState.FAILED
            run.attempts = 0
            run.latency_ms = round((perf_counter() - started) * 1000)
            run.test_output = f"workflow_error: {type(exc).__name__}: {str(exc)[:500]}"
            try:
                self.store.save_run_with_event(
                    run,
                    RunEvent(
                        run_id=run.id,
                        name="workflow.state_changed",
                        payload={
                            "from": previous_state,
                            "to": WorkflowState.FAILED.value,
                            "summary": "Unhandled workflow error stopped execution.",
                        },
                    ),
                    lease_owner=lease_owner,
                )
            except LeaseLost:
                return self.store.get_run(str(run.id)) or run
            self._event(
                run, "workflow.error", {"error_type": type(exc).__name__, "message": str(exc)[:500]}
            )
            self._event(
                run,
                "workflow.finished",
                {"status": run.status.value, "failure_category": run.failure_category.value},
            )
            self.runs[str(run.id)] = run
            return run
        if result.awaiting_approval:
            run.status = RunStatus.AWAITING_APPROVAL
        else:
            run.status = RunStatus.PASSED if result.passed else RunStatus.FAILED
        run.failure_category = result.failure_category
        run.attempts = len(result.attempts)
        run.input_tokens = result.input_tokens
        run.output_tokens = result.output_tokens
        run.cost_usd = result.cost_usd
        run.latency_ms = round((perf_counter() - started) * 1000)
        run.tool_calls = result.tool_calls
        run.outcome = result.outcome
        run.metadata["timing_ms"] = dict(result.timing_ms)
        run.metadata["counters"] = dict(result.counters)
        run.metadata["cost"] = {
            "provider_reported_usd": result.cost_usd,
            "billing_source": None,
            "note": "Provider-reported values only; Tender has no billing record.",
        }
        run.patch = result.attempts[-1].patch.diff if result.attempts else None
        run.test_output = _bounded_output(
            "\n".join(
                item.stdout + item.stderr
                for attempt in result.attempts
                for gate in attempt.quality_gates
                for item in gate.grade.results
            )
        )
        artifact_path = self.artifacts.path_for(run)
        run.metadata["artifact_path"] = str(artifact_path)
        try:
            self.artifacts.write(run, result)
        except OSError as exc:
            run.status = RunStatus.FAILED
            run.failure_category = FailureCategory.TOOLING
            run.test_output = f"artifact_error: {type(exc).__name__}: {str(exc)[:500]}"
            previous_state = run.current_state
            run.current_state = WorkflowState.FAILED
            try:
                self.store.save_run_with_event(
                    run,
                    RunEvent(
                        run_id=run.id,
                        name="workflow.state_changed",
                        payload={
                            "from": previous_state.value if previous_state else None,
                            "to": WorkflowState.FAILED.value,
                            "summary": "Required run artifacts could not be written.",
                        },
                    ),
                    lease_owner=lease_owner,
                )
            except LeaseLost:
                return self.store.get_run(str(run.id)) or run
            self._event(
                run, "artifact.error", {"error_type": type(exc).__name__, "message": str(exc)[:500]}
            )
        for attempt in result.attempts:
            self._event(
                run,
                "attempt.finished",
                {
                    "attempt": attempt.number,
                    "passed": attempt.grade.passed,
                    "reason": attempt.grade.reason,
                    "tool_calls": attempt.tool_calls,
                },
            )
        self._event(
            run,
            "workflow.finished",
            {
                "status": run.status.value,
                "failure_category": run.failure_category.value,
                "artifact_path": str(artifact_path),
            },
        )
        try:
            self.store.save_run(run, lease_owner=lease_owner)
        except LeaseLost:
            return self.store.get_run(str(run.id)) or run
        self.runs[str(run.id)] = run
        return run

    def _reject(
        self, run: EvaluationRun, reason: str, started: float, lease_owner: str | None
    ) -> EvaluationRun:
        """Finish a run that failed commit/repository policy; no provider was called."""
        previous_state = run.current_state.value if run.current_state else None
        run.status = RunStatus.FAILED
        run.failure_category = FailureCategory.POLICY
        run.current_state = WorkflowState.FAILED
        run.attempts = 0
        run.latency_ms = round((perf_counter() - started) * 1000)
        run.test_output = f"policy_rejection: {reason[:500]}"
        run.outcome = EvaluationOutcome(
            target="not_run",
            regression="not_run",
            provider="not_used",
            environment="ok",
            policy="rejected",
            reasons=[f"policy_rejection: {reason[:500]}"],
        ).finalize()
        try:
            self.store.save_run_with_event(
                run,
                RunEvent(
                    run_id=run.id,
                    name="workflow.state_changed",
                    payload={
                        "from": previous_state,
                        "to": WorkflowState.FAILED.value,
                        "summary": "Repository policy rejected the run before execution.",
                    },
                ),
                lease_owner=lease_owner,
            )
        except LeaseLost:
            return self.store.get_run(str(run.id)) or run
        self._event(run, "workflow.rejected", {"reason": reason[:500], "provider_calls": 0})
        self._event(
            run,
            "workflow.finished",
            {"status": run.status.value, "failure_category": run.failure_category.value},
        )
        self.runs[str(run.id)] = run
        return run

    def get(self, run_id: str) -> EvaluationRun | None:
        return self.store.get_run(run_id) or self.runs.get(run_id)

    def events(self, run_id: str) -> list[RunEvent]:
        return self.store.get_events(run_id)
