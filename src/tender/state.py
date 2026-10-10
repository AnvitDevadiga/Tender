from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum


class WorkflowState(StrEnum):
    TASK_RECEIVED = "task_received"
    REPOSITORY_SETUP = "repository_setup"
    REPOSITORY_ANALYSIS = "repository_analysis"
    CONTEXT_RETRIEVAL = "context_retrieval"
    PLAN = "plan"
    IMPLEMENT = "implement"
    TEST = "test"
    ANALYZE_FAILURE = "analyze_failure"
    REPLAN = "replan"
    STATIC_ANALYSIS = "static_analysis"
    DIFF_REVIEW = "diff_review"
    TASK_VERIFICATION = "task_verification"
    HUMAN_FINAL_APPROVAL = "human_final_approval"
    COMPLETE = "complete"
    FAILED = "failed"


class InvalidWorkflowTransition(ValueError):
    """Raised when orchestration attempts an invalid state transition."""


class WorkflowStateMachine:
    _allowed: dict[WorkflowState | None, frozenset[WorkflowState]] = {
        None: frozenset({WorkflowState.TASK_RECEIVED}),
        WorkflowState.TASK_RECEIVED: frozenset({WorkflowState.REPOSITORY_SETUP}),
        WorkflowState.REPOSITORY_SETUP: frozenset(
            {WorkflowState.REPOSITORY_ANALYSIS, WorkflowState.FAILED}
        ),
        WorkflowState.REPOSITORY_ANALYSIS: frozenset(
            {WorkflowState.CONTEXT_RETRIEVAL, WorkflowState.FAILED}
        ),
        WorkflowState.CONTEXT_RETRIEVAL: frozenset({WorkflowState.PLAN, WorkflowState.FAILED}),
        WorkflowState.PLAN: frozenset(
            {WorkflowState.IMPLEMENT, WorkflowState.ANALYZE_FAILURE, WorkflowState.FAILED}
        ),
        WorkflowState.IMPLEMENT: frozenset(
            {WorkflowState.TEST, WorkflowState.ANALYZE_FAILURE, WorkflowState.FAILED}
        ),
        WorkflowState.TEST: frozenset(
            {WorkflowState.STATIC_ANALYSIS, WorkflowState.ANALYZE_FAILURE, WorkflowState.FAILED}
        ),
        WorkflowState.STATIC_ANALYSIS: frozenset(
            {WorkflowState.DIFF_REVIEW, WorkflowState.ANALYZE_FAILURE, WorkflowState.FAILED}
        ),
        WorkflowState.DIFF_REVIEW: frozenset(
            {WorkflowState.TASK_VERIFICATION, WorkflowState.ANALYZE_FAILURE, WorkflowState.FAILED}
        ),
        WorkflowState.TASK_VERIFICATION: frozenset(
            {
                WorkflowState.HUMAN_FINAL_APPROVAL,
                WorkflowState.COMPLETE,
                WorkflowState.ANALYZE_FAILURE,
                WorkflowState.FAILED,
            }
        ),
        WorkflowState.HUMAN_FINAL_APPROVAL: frozenset(
            {WorkflowState.COMPLETE, WorkflowState.FAILED}
        ),
        WorkflowState.ANALYZE_FAILURE: frozenset({WorkflowState.REPLAN, WorkflowState.FAILED}),
        WorkflowState.REPLAN: frozenset({WorkflowState.CONTEXT_RETRIEVAL}),
        WorkflowState.COMPLETE: frozenset(),
        WorkflowState.FAILED: frozenset(),
    }

    def __init__(
        self,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.state: WorkflowState | None = None
        self.event_sink = event_sink

    def transition(self, target: WorkflowState, summary: str) -> None:
        if target not in self._allowed[self.state]:
            previous = self.state.value if self.state else "uninitialized"
            raise InvalidWorkflowTransition(f"cannot transition from {previous} to {target.value}")
        previous = self.state.value if self.state else None
        self.state = target
        if self.event_sink:
            self.event_sink(
                "workflow.state_changed",
                {"from": previous, "to": target.value, "summary": summary[:500]},
            )
