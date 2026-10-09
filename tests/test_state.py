import pytest

from forgebench.state import (
    InvalidWorkflowTransition,
    WorkflowState,
    WorkflowStateMachine,
)


def test_workflow_state_machine_rejects_invalid_transitions() -> None:
    state_machine = WorkflowStateMachine()

    with pytest.raises(InvalidWorkflowTransition, match="uninitialized to test"):
        state_machine.transition(WorkflowState.TEST, "invalid jump")

    state_machine.transition(WorkflowState.TASK_RECEIVED, "received")
    with pytest.raises(InvalidWorkflowTransition, match="task_received to complete"):
        state_machine.transition(WorkflowState.COMPLETE, "skip workflow")


def test_workflow_state_machine_records_transition_summary() -> None:
    events = []
    state_machine = WorkflowStateMachine(lambda name, payload: events.append((name, payload)))

    state_machine.transition(WorkflowState.TASK_RECEIVED, "accepted")
    state_machine.transition(WorkflowState.REPOSITORY_SETUP, "copy")

    assert events == [
        (
            "workflow.state_changed",
            {"from": None, "to": "task_received", "summary": "accepted"},
        ),
        (
            "workflow.state_changed",
            {"from": "task_received", "to": "repository_setup", "summary": "copy"},
        ),
    ]
