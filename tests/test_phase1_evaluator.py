"""Phase 1 regression tests: evaluator correctness and security boundaries.

Each test drives real Git repositories in temporary directories with
deterministic fake providers; nothing reaches a network or a paid API.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib import request

import pytest

from tender import net
from tender.integrity import EvaluatorAssets, IntegrityPolicy, check_patch_paths
from tender.models import BenchmarkConfig, EvaluationRun, FailureCategory, Issue, RunStatus
from tender.outcomes import CheckOutcome, OverallStatus
from tender.providers import ModelResponse, OpenAICompatibleProvider, ToolCall
from tender.repository import RepositoryError, RepositoryManager
from tender.services import RunService
from tender.snapshot import RepositoryBoundaryError, TrustedRepository
from tender.store import RunStore
from tender.tools import ALL_TOOL_NAMES, AgentToolbox
from tender.workflow import AgentWorkflow

PLAN = '{"summary":"Fix it","steps":["Edit app.py"],"target_files":["app.py"]}'
PY = sys.executable


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None) -> str:
    path.mkdir(parents=True)
    for name, content in (files or {"app.py": "return 1\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Tender Test")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return git(path, "rev-parse", "HEAD")


def assert_source_untouched(path: Path, commit: str) -> None:
    assert git(path, "rev-parse", "HEAD") == commit
    assert git(path, "status", "--porcelain", "--untracked-files=all") == ""


class ScriptedDiffProvider:
    """Return a fixed plan and then one scripted unified diff per attempt."""

    def __init__(self, diffs: list[str]) -> None:
        self.diffs = list(diffs)
        self.calls = 0

    async def generate(self, messages, tools=None):
        self.calls += 1
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(PLAN)
        return ModelResponse(self.diffs.pop(0))


def diff(path: str, old: str, new: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def new_file(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def delete_file(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "".join(f"-{line}\n" for line in lines)
    return f"--- a/{path}\n+++ /dev/null\n@@ -1,{len(lines)} +0,0 @@\n{body}"


EXPECT_TWO = f"{PY} -c \"assert open('app.py').read().strip() == 'return 2'\""


# ---------------------------------------------------------------- 1.1 pristine


@pytest.mark.asyncio
async def test_failed_attempt_cannot_contaminate_repaired_verification(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    # A. the first patch is wrong and leaves an extra file behind.
    first = diff("app.py", "return 1", "return 3") + new_file("notes.txt", "scratch")
    # B. the agent repairs it: fix the value and delete its scratch file.
    second = diff("app.py", "return 3", "return 2") + delete_file("notes.txt", "scratch")
    provider = ScriptedDiffProvider([first, second])
    events: list[tuple[str, dict]] = []
    no_notes = f"{PY} -c \"import os; assert not os.path.exists('notes.txt')\""

    result = await AgentWorkflow(provider).run(
        Issue(repository="o/r", title="fix", body="return 2"),
        agent,
        [f"{PY} -c 'pass'"],
        max_retries=1,
        event_sink=lambda name, payload: events.append((name, payload)),
        verification_commands=[EXPECT_TWO, no_notes],
        verification_workspace=source,
    )

    # C. the repaired candidate verifies.
    assert result.passed is True
    assert [attempt.grade.reason for attempt in result.attempts] == [
        "task_verification_failed",
        "task_verification_passed",
    ]
    # D. both verifications started from identical, pristine base-commit trees.
    targets = [
        next(record for record in attempt.verification if record["kind"] == "target")
        for attempt in result.attempts
    ]
    assert all(record["pristine"] for record in targets)
    assert targets[0]["pre_patch"] == targets[1]["pre_patch"]
    assert targets[0]["pre_patch"]["head"] == commit
    assert targets[0]["passed"] is False and targets[1]["passed"] is True
    assert result.outcome is not None and result.outcome.overall is OverallStatus.ACCEPTED
    assert result.outcome.independently_verified is True
    # E. the source repository is unchanged.
    assert_source_untouched(source, commit)
    assert (source / "app.py").read_text(encoding="utf-8") == "return 1\n"
    leftovers = [
        name for name in os.listdir(tempfile.gettempdir()) if name.startswith("tender-verify-")
    ]
    assert leftovers == []


@pytest.mark.asyncio
async def test_verifier_ignores_untracked_and_ignored_files_in_the_source(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source, {"app.py": "return 1\n", ".gitignore": ".env\n"})
    (source / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    agent = tmp_path / "agent"
    with TrustedRepository(source) as repository:
        repository.materialize(commit, agent)
    assert not (agent / ".env").exists()
    no_env = f"{PY} -c \"import os; assert not os.path.exists('.env')\""

    result = await AgentWorkflow(
        ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    ).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        verification_commands=[EXPECT_TWO, no_env],
        verification_workspace=source,
        base_commit=commit,
    )

    assert result.passed is True
    assert (source / ".env").read_text(encoding="utf-8") == "TOKEN=do-not-copy\n"


@pytest.mark.asyncio
async def test_regression_checks_run_in_their_own_pristine_workspace(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source, {"app.py": "return 1\n", "other.py": "VALUE = 7\n"})
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    regression = f"{PY} -c \"assert open('other.py').read() == 'VALUE = 7\\n'\""
    # The target check leaves a file behind; the regression verifier must not see it.
    target = f"{EXPECT_TWO} && touch leaked.flag"
    no_leak = f"{PY} -c \"import os; assert not os.path.exists('leaked.flag')\""

    result = await AgentWorkflow(
        ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    ).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        verification_commands=[target],
        regression_commands=[regression, no_leak],
        verification_workspace=source,
    )

    assert result.passed is True
    assert result.outcome.target is CheckOutcome.PASSED
    assert result.outcome.regression is CheckOutcome.PASSED
    kinds = [record["kind"] for record in result.attempts[0].verification]
    assert kinds == ["integrity", "target", "regression"]


@pytest.mark.asyncio
async def test_regression_failure_is_reported_separately_from_target(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source, {"app.py": "return 1\n", "other.py": "VALUE = 7\n"})
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    breaking = diff("app.py", "return 1", "return 2") + diff("other.py", "VALUE = 7", "VALUE = 8")
    regression = f"{PY} -c \"assert open('other.py').read() == 'VALUE = 7\\n'\""

    result = await AgentWorkflow(ScriptedDiffProvider([breaking])).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        regression_commands=[regression],
        verification_workspace=source,
    )

    assert result.passed is False
    assert result.outcome.target is CheckOutcome.PASSED
    assert result.outcome.regression is CheckOutcome.FAILED
    assert result.outcome.overall is OverallStatus.REJECTED
    assert result.failure_category is FailureCategory.TEST


# ---------------------------------------------------------- 1.2 commit identity


def service_run(source: Path, base_commit: str | None, **config) -> EvaluationRun:
    return EvaluationRun(
        issue=Issue(repository="o/r", title="fix", body="fix", base_commit=base_commit),
        config=BenchmarkConfig(commands=[EXPECT_TWO], max_retries=0, **config),
    )


@pytest.mark.asyncio
async def test_matching_commit_is_executed_and_recorded(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    provider = ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    service = RunService(provider, RunStore(":memory:"), artifacts=_artifacts(tmp_path))

    run = await service.execute_workflow(service_run(source, commit), str(source))

    assert run.status is RunStatus.PASSED
    assert run.metadata["requested_base_commit"] == commit
    assert run.metadata["executed_base_commit"] == commit
    assert run.outcome.verification_commit == commit
    isolated = next(
        event for event in service.events(str(run.id)) if event.name == "workspace.isolated"
    )
    assert isolated.payload["base_commit"] == commit
    assert run.metadata["source_unchanged"] is True
    assert_source_untouched(source, commit)


@pytest.mark.asyncio
async def test_mismatched_commit_is_rejected_before_any_provider_call(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    provider = ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    service = RunService(provider, RunStore(":memory:"), artifacts=_artifacts(tmp_path))

    run = await service.execute_workflow(service_run(source, "0" * 40), str(source))

    assert run.status is RunStatus.FAILED
    assert run.failure_category is FailureCategory.POLICY
    assert provider.calls == 0
    assert run.outcome.policy == "rejected" and run.outcome.provider == "not_used"
    assert "does not match repository HEAD" in run.test_output
    assert "executed_base_commit" not in run.metadata
    names = [event.name for event in service.events(str(run.id))]
    assert "workflow.rejected" in names and "plan.created" not in names


@pytest.mark.asyncio
async def test_missing_commit_is_rejected_when_pinning_is_required(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    provider = ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    service = RunService(
        provider, RunStore(":memory:"), artifacts=_artifacts(tmp_path), require_pinned_commit=True
    )

    run = await service.execute_workflow(service_run(source, None), str(source))

    assert run.status is RunStatus.FAILED
    assert run.failure_category is FailureCategory.POLICY
    assert provider.calls == 0
    assert "commit pinning" in run.test_output


@pytest.mark.asyncio
async def test_missing_commit_is_resolved_and_recorded_when_pinning_is_optional(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    provider = ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    service = RunService(provider, RunStore(":memory:"), artifacts=_artifacts(tmp_path))

    run = await service.execute_workflow(service_run(source, None), str(source))

    assert run.status is RunStatus.PASSED
    assert run.metadata["requested_base_commit"] is None
    assert run.metadata["executed_base_commit"] == commit


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["dirty", "untracked", "symlink"])
async def test_ambiguous_or_unsafe_repository_state_is_rejected(tmp_path, problem) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    if problem == "dirty":
        (source / "app.py").write_text("return 9\n", encoding="utf-8")
    elif problem == "untracked":
        (source / "stray.py").write_text("x = 1\n", encoding="utf-8")
    else:
        (source / "link").symlink_to(tmp_path)
        git(source, "add", "link")
        git(source, "commit", "-qm", "link")
        commit = git(source, "rev-parse", "HEAD")
    provider = ScriptedDiffProvider([diff("app.py", "return 1", "return 2")])
    service = RunService(provider, RunStore(":memory:"), artifacts=_artifacts(tmp_path))

    run = await service.execute_workflow(service_run(source, commit), str(source))

    assert run.status is RunStatus.FAILED
    assert run.failure_category is FailureCategory.POLICY
    assert provider.calls == 0


def _artifacts(tmp_path: Path):
    from tender.artifacts import ArtifactStore

    return ArtifactStore(tmp_path / "artifacts")


# --------------------------------------------------------- 1.3 tool authorization


class ToolScriptProvider:
    """Plan, then request scripted tool calls regardless of what was advertised."""

    def __init__(self, calls: list[ToolCall], allowlist=None, finish: str = "done") -> None:
        self.script = list(calls)
        self.finish = finish
        self.advertised: list[set[str]] = []
        self.calls = 0
        if allowlist is not None:
            self.tool_allowlist = frozenset(allowlist)

    async def generate(self, messages, tools=None):
        self.calls += 1
        if "implementation plan" in messages[0]["content"].lower():
            return ModelResponse(PLAN)
        self.advertised.append({tool["function"]["name"] for tool in tools or []})
        if self.script:
            return ModelResponse("", tool_calls=(self.script.pop(0),))
        return ModelResponse(self.finish)


async def run_tool_workflow(tmp_path: Path, provider) -> tuple[object, list, Path]:
    source = tmp_path / "source"
    make_repo(source)
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    events: list[tuple[str, dict]] = []
    result = await AgentWorkflow(provider).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [EXPECT_TWO],
        max_retries=0,
        event_sink=lambda name, payload: events.append((name, payload)),
        verification_workspace=source,
    )
    return result, events, agent


@pytest.mark.asyncio
async def test_unadvertised_tool_call_is_rejected_before_execution(tmp_path) -> None:
    write = ToolCall("w1", "write_file", '{"path":"app.py","content":"return 2\\n"}')
    provider = ToolScriptProvider([write], allowlist={"read_file"})

    result, events, agent = await run_tool_workflow(tmp_path, provider)

    assert provider.advertised[0] == {"read_file"}
    assert (agent / "app.py").read_text(encoding="utf-8") == "return 1\n"
    rejected = [payload for name, payload in events if name == "agent_tool.rejected"]
    assert rejected == [
        {
            "attempt": 1,
            "call_id": "w1",
            "name": "write_file",
            "reason": "tool is not authorized: write_file",
            "allowed_tools": ["read_file"],
        }
    ]
    assert not any(name == "agent_tool.completed" for name, _ in events)
    assert result.passed is False
    assert result.counters["rejected_tool_calls"] == 1


@pytest.mark.asyncio
async def test_run_command_is_never_implicitly_allowed(tmp_path) -> None:
    marker = tmp_path / "executed.flag"
    command = ToolCall("c1", "run_command", json.dumps({"command": f"touch {marker}"}))
    provider = ToolScriptProvider([command])  # no allowlist declared

    result, events, _ = await run_tool_workflow(tmp_path, provider)

    assert "run_command" not in provider.advertised[0]
    assert not marker.exists()
    assert any(
        name == "agent_tool.rejected" and payload["name"] == "run_command"
        for name, payload in events
    )


@pytest.mark.asyncio
async def test_repeated_unauthorized_calls_become_a_policy_rejection(tmp_path) -> None:
    calls = [ToolCall(f"x{index}", "run_command", '{"command":"true"}') for index in range(3)]
    provider = ToolScriptProvider(calls, allowlist={"read_file", "replace_text"})

    result, events, _ = await run_tool_workflow(tmp_path, provider)

    assert result.passed is False
    assert result.failure_category is FailureCategory.POLICY
    assert result.outcome.policy == "rejected"
    assert result.outcome.overall is OverallStatus.REJECTED
    assert sum(name == "agent_tool.rejected" for name, _ in events) == 3


@pytest.mark.asyncio
async def test_malformed_and_unknown_tool_calls_fail_safely(tmp_path) -> None:
    calls = [
        ToolCall("m1", "read_file", "{not json"),
        ToolCall("m2", "rm_rf", '{"path":"/"}'),
        ToolCall(
            "m3", "replace_text", '{"path":"app.py","old_text":"return 1","new_text":"return 2"}'
        ),
    ]
    provider = ToolScriptProvider(calls, allowlist={"read_file", "replace_text"})

    result, events, agent = await run_tool_workflow(tmp_path, provider)

    completed = [payload for name, payload in events if name == "agent_tool.completed"]
    rejected = [payload for name, payload in events if name == "agent_tool.rejected"]
    assert completed[0]["name"] == "read_file" and completed[0]["ok"] is False
    assert rejected[0]["name"] == "rm_rf" and "unknown tool" in rejected[0]["reason"]
    assert result.passed is True  # the authorized edit still went through verification
    assert (agent / "app.py").read_text(encoding="utf-8") == "return 2\n"


@pytest.mark.parametrize("tool", sorted(ALL_TOOL_NAMES - {"read_file"}))
def test_toolbox_rejects_every_tool_outside_its_allowlist(tmp_path, tool) -> None:
    root = tmp_path / "repo"
    make_repo(root)
    toolbox = AgentToolbox(
        root,
        sandbox_backend="local",
        timeout_seconds=5,
        test_commands=["true"],
        setup_commands=[],
        static_analysis_commands=["true"],
        allowed_tools={"read_file"},
    )

    result = toolbox.execute(tool, "{}")

    assert result.rejected is True and result.ok is False
    assert "not authorized" in result.content
    assert {item["function"]["name"] for item in toolbox.advertised()} == {"read_file"}


# ------------------------------------------------------ 1.4 evaluator integrity


TEST_FILE = "from app import value\n\n\ndef test_value():\n    assert value() == 2\n"
PYTEST = f"{PY} -m pytest -q -p no:cacheprovider tests/test_app.py"


def integrity_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    commit = make_repo(
        source, {"app.py": "def value():\n    return 1\n", "tests/test_app.py": TEST_FILE}
    )
    agent = tmp_path / "agent"
    subprocess.run(["git", "clone", "-q", str(source), str(agent)], check=True)
    return source, agent, commit


TAMPER_TESTS = (
    "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -4,2 +4,2 @@\n"
    " def test_value():\n-    assert value() == 2\n+    assert True\n"
)
FORCE_PASS_CONFTEST = new_file(
    "conftest.py",
    "import pytest\n\n\n@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    outcome.get_result().outcome = 'passed'\n",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [TAMPER_TESTS, FORCE_PASS_CONFTEST], ids=["tests", "conftest"])
async def test_tampering_with_evaluator_tests_cannot_produce_acceptance(tmp_path, tamper) -> None:
    source, agent, commit = integrity_repo(tmp_path)
    provider = ScriptedDiffProvider([tamper])

    result = await AgentWorkflow(provider).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [PYTEST],  # the agent's own loop sees the tampered tests and passes
        max_retries=0,
        verification_workspace=source,
    )

    gates = {gate.name: gate.grade.passed for gate in result.attempts[0].quality_gates}
    assert gates["tests"] is True
    assert gates["task_verification"] is False
    target = next(r for r in result.attempts[0].verification if r["kind"] == "target")
    assert target["restored_from_base"] or target["removed_patch_additions"]
    assert result.passed is False
    assert result.outcome.overall is OverallStatus.REJECTED
    assert_source_untouched(source, commit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["pytest.py", "sitecustomize.py", "pkg/evil.pth", ".tender/evaluator/run.toml"]
)
async def test_protected_or_tool_shadowing_paths_are_never_verified(tmp_path, path) -> None:
    source, agent, _ = integrity_repo(tmp_path)
    tamper = new_file(path, "raise SystemExit(0)")

    result = await AgentWorkflow(ScriptedDiffProvider([tamper])).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [f"{PY} -c 'pass'"],
        max_retries=0,
        verification_workspace=source,
    )

    assert result.passed is False
    assert result.attempts[0].grade.reason.startswith(
        ("integrity_violation", "protected_files_modified")
    )
    assert result.failure_category is FailureCategory.INTEGRITY
    assert "task_verification" not in {gate.name for gate in result.attempts[0].quality_gates}


@pytest.mark.asyncio
async def test_hidden_evaluator_assets_are_injected_and_cannot_be_overwritten(tmp_path) -> None:
    source, agent, _ = integrity_repo(tmp_path)
    hidden = tmp_path / "hidden"
    (hidden / "tests").mkdir(parents=True)
    (hidden / "tests" / "test_hidden.py").write_text(
        "from app import value\n\n\ndef test_hidden():\n    assert value() == 2\n", encoding="utf-8"
    )
    assets = EvaluatorAssets.from_directory(hidden)
    fix = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def value():\n-    return 1\n+    return 2\n"
    )
    hidden_cmd = f"{PY} -m pytest -q -p no:cacheprovider tests/test_hidden.py"

    passed = await AgentWorkflow(ScriptedDiffProvider([fix])).run(
        Issue(repository="o/r", title="fix", body="fix"),
        agent,
        [PYTEST],
        max_retries=0,
        verification_commands=[hidden_cmd],
        verification_workspace=source,
        evaluator_assets=assets,
    )
    assert passed.passed is True
    integrity = passed.attempts[0].verification[0]
    assert integrity["evaluator_assets"][0]["path"] == "tests/test_hidden.py"
    assert not (agent / "tests" / "test_hidden.py").exists()  # never visible to the agent

    findings = check_patch_paths(("tests/test_hidden.py",), IntegrityPolicy(), assets)
    assert findings[0].rule == "evaluator_asset"


# ------------------------------------------------------- 1.5 host-side safety


def hostile_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "hostile"
    make_repo(source, {"a.txt": "hi\n", ".gitattributes": "*.txt filter=evil\n"})
    markers = tmp_path / "markers"
    markers.mkdir()
    git(source, "config", "core.fsmonitor", f"touch {markers}/fsmonitor; false")
    git(source, "config", "filter.evil.clean", f"touch {markers}/clean; cat")
    git(source, "config", "filter.evil.smudge", f"touch {markers}/smudge; cat")
    git(source, "config", "diff.external", f"touch {markers}/external")
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {markers}/hook\n", encoding="utf-8")
    hook.chmod(0o755)
    (source / "a.txt").write_text("hi\nchanged\n", encoding="utf-8")
    return source, markers


def test_repository_helpers_never_execute_on_the_host(tmp_path) -> None:
    source, markers = hostile_repo(tmp_path)

    with TrustedRepository(source) as repository:
        identity = repository.identity()
        head = repository.head_commit()
        workspace = repository.materialize(head, tmp_path / "workspace")

    assert identity.dirty is True
    assert sorted(os.listdir(markers)) == []
    assert not (workspace / ".git" / "hooks").exists()
    config = (workspace / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in config and "filter" not in config and "remote" not in config
    with pytest.raises(RepositoryError, match="must be clean"):
        RepositoryManager.validate_clean_git_repository(source)
    assert sorted(os.listdir(markers)) == []


def test_linked_or_symlinked_git_metadata_is_rejected(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    linked = tmp_path / "linked"
    git(source, "worktree", "add", "-q", str(linked))
    with pytest.raises(RepositoryBoundaryError, match="standalone"):
        TrustedRepository(linked).open()

    symlinked = tmp_path / "symlinked"
    symlinked.mkdir()
    (symlinked / ".git").symlink_to(source / ".git")
    with pytest.raises(RepositoryBoundaryError, match="standalone"):
        TrustedRepository(symlinked).open()

    (source / ".git" / "info" / "escape").symlink_to(tmp_path)
    with pytest.raises(RepositoryBoundaryError, match="symbolic link"):
        TrustedRepository(source).open()


def test_object_alternates_and_unknown_commits_are_rejected(tmp_path) -> None:
    source = tmp_path / "source"
    make_repo(source)
    with TrustedRepository(source) as repository:
        with pytest.raises(RepositoryBoundaryError, match="not found"):
            repository.resolve_commit("f" * 40)
        with pytest.raises(RepositoryBoundaryError, match="revision"):
            repository.resolve_commit("--output=/tmp/x")
    (source / ".git" / "objects" / "info" / "alternates").write_text("/elsewhere\n")
    with pytest.raises(RepositoryBoundaryError, match="alternates"):
        TrustedRepository(source).open()


def test_submodule_entries_are_rejected_before_checkout(tmp_path) -> None:
    source = tmp_path / "source"
    commit = make_repo(source)
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    git(source, "commit", "-qm", "submodule")
    with TrustedRepository(source) as repository:
        with pytest.raises(RepositoryBoundaryError, match="submodule"):
            repository.materialize(repository.head_commit(), tmp_path / "ws")


def test_remote_repository_sources_are_disabled(tmp_path) -> None:
    manager = RepositoryManager(tmp_path / "snapshots")
    for source in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ext::sh -c id"):
        with pytest.raises(RepositoryError, match="remote repository sources are disabled"):
            manager.snapshot(source, "copy")


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []
    target = ""

    def do_GET(self):  # noqa: N802
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization", "")})
        if self.path == "/start":
            self.send_response(302)
            self.send_header("Location", type(self).target)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # noqa: D401
        return None


def test_credentialed_requests_never_follow_redirects() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        _Recorder.seen = []
        _Recorder.target = f"{base}/elsewhere"
        req = request.Request(f"{base}/start", headers={"Authorization": "Bearer secret-token"})
        with pytest.raises(net.RedirectRefused):
            net.open_url(req, timeout=5)
        assert [entry["path"] for entry in _Recorder.seen] == ["/start"]
    finally:
        server.shutdown()


@pytest.mark.asyncio
async def test_provider_refuses_to_send_keys_over_plain_http() -> None:
    provider = OpenAICompatibleProvider(api_key="placeholder", base_url="http://example.com/v1")
    with pytest.raises(RuntimeError, match="https"):
        await provider.generate([{"role": "user", "content": "hi"}])
    local = OpenAICompatibleProvider(api_key="placeholder", base_url="http://127.0.0.1:9/v1")
    net.require_secure_endpoint(local.endpoint)  # loopback servers stay supported
