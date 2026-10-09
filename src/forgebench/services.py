from __future__ import annotations

import tempfile
from pathlib import Path
from time import perf_counter

from .artifacts import ArtifactStore
from .models import EvaluationRun, FailureCategory, RunEvent, RunStatus
from .providers import DeterministicProvider, ModelProvider
from .repository import RepositoryManager
from .state import WorkflowState
from .store import LeaseLost, RunStore
from .workflow import AgentWorkflow


class RunService:
    def __init__(
        self,
        provider: ModelProvider | None = None,
        store: RunStore | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.provider = provider or DeterministicProvider()
        self.store = store or RunStore()
        self.artifacts = artifacts or ArtifactStore()
        self.runs: dict[str, EvaluationRun] = {}

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
            base_commit = RepositoryManager.validate_clean_git_repository(workspace)
            run.metadata["base_commit"] = base_commit
            with tempfile.TemporaryDirectory(prefix="forgebench-run-") as temp_dir:
                isolated_workspace = RepositoryManager.isolated_copy(
                    workspace, Path(temp_dir) / "workspace"
                )
                verification_workspace = RepositoryManager.isolated_copy(
                    workspace, Path(temp_dir) / "verification"
                )
                self._event(
                    run,
                    "workspace.isolated",
                    {
                        "copy": "created",
                        "source_modified": False,
                        "base_commit": base_commit,
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
                    verification_workspace=verification_workspace,
                    require_human_approval=run.config.require_human_approval,
                )
        except LeaseLost:
            return self.store.get_run(str(run.id)) or run
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
        run.patch = result.attempts[-1].patch.diff if result.attempts else None
        run.test_output = "\n".join(
            item.stdout + item.stderr
            for attempt in result.attempts
            for gate in attempt.quality_gates
            for item in gate.grade.results
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

    def get(self, run_id: str) -> EvaluationRun | None:
        return self.store.get_run(run_id) or self.runs.get(run_id)

    def events(self, run_id: str) -> list[RunEvent]:
        return self.store.get_events(run_id)
