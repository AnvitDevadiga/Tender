import subprocess

import pytest

from tender.artifacts import ArtifactStore
from tender.models import BenchmarkConfig, EvaluationRun, Issue, RunStatus
from tender.providers import ModelResponse
from tender.services import RunService
from tender.state import WorkflowState
from tender.store import RunStore


class FixProvider:
    async def generate(self, messages, tools=None):
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(
                '{"summary":"Fix the return value","steps":["Update answer"],'
                '"target_files":["app.py"]}',
                input_tokens=4,
                output_tokens=6,
            )
        return ModelResponse(
            "```diff\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n```",
            input_tokens=10,
            output_tokens=12,
        )


def make_repo(path) -> None:
    path.mkdir()
    (path / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Tender Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


@pytest.mark.asyncio
async def test_phase4_workflow_persists_auditable_result(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    store = RunStore(tmp_path / "runs.db")
    service = RunService(
        provider=FixProvider(),
        store=store,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Fix return value", body="return 2"),
        config=BenchmarkConfig(
            commands=['python3 -c \'assert open("app.py").read().strip() == "return 2"\''],
            static_analysis_commands=["python3 -c 'assert True'"],
            verification_commands=[
                'python3 -c \'assert open("app.py").read().strip() == "return 2"\''
            ],
            max_retries=0,
        ),
        benchmark_id="phase4-test",
    )

    result = await service.execute_workflow(run, str(repo))

    assert result.status is RunStatus.PASSED
    assert result.current_state.value == "complete"
    assert (repo / "app.py").read_text(encoding="utf-8") == "return 1\n"
    assert result.tool_calls >= 4
    assert result.metadata["artifact_path"]
    assert (tmp_path / "artifacts" / str(result.id) / "run.json").is_file()
    assert (tmp_path / "artifacts" / str(result.id) / "attempt-1" / "quality_gates.json").is_file()
    event_names = [event.name for event in service.events(str(result.id))]
    assert "retrieval.completed" in event_names
    assert "plan.created" in event_names
    assert "provider.completed" in event_names
    assert "grader.completed" in event_names
    assert "static_analysis.completed" in event_names
    assert "diff_review.completed" in event_names
    assert "task_verification.completed" in event_names
    grader_event = next(
        event for event in service.events(str(result.id)) if event.name == "grader.completed"
    )
    assert grader_event.payload["checks"][0]["passed"] is True
    state_events = [
        event for event in service.events(str(result.id)) if event.name == "workflow.state_changed"
    ]
    assert state_events[-1].payload["to"] == "complete"


def test_memory_store_round_trip() -> None:
    store = RunStore(":memory:")
    run = EvaluationRun(issue=Issue(repository="o/r", title="memory"))
    store.save_run(run)
    assert store.get_run(str(run.id)) is not None


@pytest.mark.asyncio
async def test_configured_final_approval_keeps_verified_run_pending(tmp_path) -> None:
    repo = tmp_path / "repo"
    make_repo(repo)
    service = RunService(
        provider=FixProvider(),
        store=RunStore(tmp_path / "runs.db"),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )
    run = EvaluationRun(
        issue=Issue(repository="local/repo", title="Fix return value", body="return 2"),
        config=BenchmarkConfig(
            commands=['python3 -c \'assert open("app.py").read().strip() == "return 2"\''],
            require_human_approval=True,
            max_retries=0,
        ),
    )

    result = await service.execute_workflow(run, str(repo))

    assert result.status is RunStatus.AWAITING_APPROVAL
    assert result.current_state is WorkflowState.HUMAN_FINAL_APPROVAL
    assert service.events(str(result.id))[-1].name == "workflow.finished"
    assert result.metadata["artifact_path"]
