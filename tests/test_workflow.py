import shutil
import subprocess

import pytest

from forgebench.models import Issue
from forgebench.patches import PatchCapture
from forgebench.providers import ModelResponse, ToolCall
from forgebench.sandbox import SandboxUnavailable
from forgebench.workflow import AgentPlan, AgentWorkflow


class PatchProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools=None):
        self.calls += 1
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(
                '{"summary":"Fix the return value","steps":["Change the return value"],'
                '"target_files":["app.py"]}'
            )
        return ModelResponse(
            "```diff\n--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,1 @@\n-return 1\n+return 2\n```"
        )


class RateLimitedOnceProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("Groq API rejected the request (429): Please try again in 8.5s")
        return ModelResponse("ok")


@pytest.mark.asyncio
async def test_provider_retry_handles_one_rate_limit(monkeypatch) -> None:
    delays = []

    async def no_wait(_seconds: float) -> None:
        delays.append(_seconds)
        return None

    monkeypatch.setattr("forgebench.workflow.asyncio.sleep", no_wait)
    provider = RateLimitedOnceProvider()
    workflow = AgentWorkflow(provider)
    result = await workflow._generate_with_provider_retry([])
    assert result.text == "ok"
    assert provider.calls == 2
    assert delays == [8.75]


@pytest.mark.asyncio
async def test_provider_retry_parses_millisecond_rate_limit(monkeypatch) -> None:
    delays = []

    async def no_wait(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("forgebench.workflow.asyncio.sleep", no_wait)

    class MillisecondRateLimitProvider:
        calls = 0

        async def generate(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Groq API rejected the request (429): Please try again in 500ms")
            return ModelResponse("ok")

    provider = MillisecondRateLimitProvider()
    result = await AgentWorkflow(provider)._generate_with_provider_retry([])
    assert result.text == "ok"
    assert delays == [0.75]


class ToolCallingProvider:
    def __init__(self) -> None:
        self.inspected = False
        self.edited = False

    async def generate(self, messages, tools=None):
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(
                '{"summary":"Fix the return value","steps":["Inspect and update app.py"],'
                '"target_files":["app.py"]}'
            )
        if not self.inspected:
            self.inspected = True
            assert tools is not None
            return ModelResponse(
                "",
                tool_calls=(ToolCall("read-1", "read_file", '{"path":"app.py"}'),),
            )
        assert messages[-1]["role"] == "tool"
        if not self.edited:
            self.edited = True
            return ModelResponse(
                "",
                tool_calls=(
                    ToolCall(
                        "replace-1",
                        "replace_text",
                        '{"path":"app.py","old_text":"return 1",'
                        '"new_text":"return 2","expected_replacements":1}',
                    ),
                ),
            )
        return ModelResponse("Updated the return value as requested.")


def test_plan_rejects_paths_outside_the_repository(tmp_path) -> None:
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the repository"):
        AgentPlan.parse(
            '{"summary":"escape","steps":["read"],"target_files":["../secret"]}',
            tmp_path,
        )


def test_plan_drops_unknown_files_and_keeps_only_existing_repo_files(tmp_path) -> None:
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    plan = AgentPlan.parse(
        '{"summary":"fix","steps":["update it"],"target_files":["app.py","missing.py"]}',
        tmp_path,
    )
    assert plan.target_files == ("app.py",)


def test_static_analysis_rejects_whitespace_errors(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("return 1 \n", encoding="utf-8")
    patch = PatchCapture().capture(tmp_path)

    grade = AgentWorkflow._static_analysis(
        AgentWorkflow(PatchProvider()), tmp_path, patch, [], [], "local", 5
    )

    assert grade.passed is False
    assert grade.reason == "static_analysis_failed"


@pytest.mark.asyncio
async def test_workflow_applies_patch_and_grades(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    states = []
    verification_workspace = tmp_path.parent / f"{tmp_path.name}-verification"
    shutil.copytree(tmp_path, verification_workspace)
    result = await AgentWorkflow(PatchProvider()).run(
        Issue(repository="o/r", title="fix", body="fix"),
        tmp_path,
        ['python3 -c \'assert open("app.py").read().strip() == "return 2"\''],
        0,
        event_sink=lambda name, payload: (
            states.append(payload["to"]) if name == "workflow.state_changed" else None
        ),
        verification_workspace=verification_workspace,
    )
    assert result.passed is True
    assert result.attempts[0].patch.sha256
    assert states == [
        "task_received",
        "repository_setup",
        "repository_analysis",
        "context_retrieval",
        "plan",
        "implement",
        "test",
        "static_analysis",
        "diff_review",
        "task_verification",
        "complete",
    ]


@pytest.mark.asyncio
async def test_workflow_executes_and_audits_structured_repository_tool_calls(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    verification_workspace = tmp_path.parent / f"{tmp_path.name}-tool-verification"
    shutil.copytree(tmp_path, verification_workspace)
    events = []

    result = await AgentWorkflow(ToolCallingProvider()).run(
        Issue(repository="o/r", title="fix return", body="return 2"),
        tmp_path,
        ['python3 -c \'assert open("app.py").read().strip() == "return 2"\''],
        max_retries=0,
        event_sink=lambda name, payload: events.append((name, payload)),
        verification_workspace=verification_workspace,
    )

    assert result.passed is True
    assert result.tool_calls >= 5
    tool_events = [payload for name, payload in events if name == "agent_tool.completed"]
    assert [event["name"] for event in tool_events] == ["read_file", "replace_text"]
    assert all(event["ok"] is True and event["duration_ms"] >= 0 for event in tool_events)
    assert tool_events[0]["input"] == {"path": "app.py"}
    assert "return 1" not in str(tool_events[1]["input"])
    assert "old_text_sha256" in tool_events[1]["input"]


@pytest.mark.asyncio
async def test_workflow_stops_retrying_when_docker_is_unavailable(tmp_path, monkeypatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "ForgeBench Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    class MissingDocker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, command: str):
            raise SandboxUnavailable("Docker daemon is unavailable")

        def destroy(self) -> None:
            pass

    monkeypatch.setattr("forgebench.workflow.DockerSandbox", MissingDocker)
    provider = PatchProvider()
    result = await AgentWorkflow(provider).run(
        Issue(repository="o/r", title="fix", body="fix"),
        tmp_path,
        ["true"],
        max_retries=3,
        sandbox_backend="docker",
    )

    assert result.passed is False
    assert result.failure_category.value == "environment_setup"
    assert len(result.attempts) == 1
    assert provider.calls == 2
    assert result.attempts[0].patch.diff
