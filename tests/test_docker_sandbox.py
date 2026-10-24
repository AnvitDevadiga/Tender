"""Live Docker sandbox checks. Skipped unless a daemon and tender-sandbox:local exist.

Build the image first: docker build -f Dockerfile.sandbox -t tender-sandbox:local .
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from tender.patch_eval import docker_available
from tender.sandbox import DockerSandbox, ExecutionBudget, current_budget

IMAGE = "tender-sandbox:local"


def _image_present() -> bool:
    if not docker_available():
        return False
    return (
        subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.skipif(not _image_present(), reason="Docker daemon or image unavailable")


def _containers() -> set[str]:
    listing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"ancestor={IMAGE}"], capture_output=True, text=True
    )
    return set(listing.stdout.split())


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path


def test_container_is_isolated_and_limited(workspace) -> None:
    sandbox = DockerSandbox(workspace, image=IMAGE, timeout_seconds=30)
    try:
        identity = sandbox.run("id -u; id -g")
        network = sandbox.run(
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
        )
        root_write = sandbox.run("touch /usr/tender-probe")
        input_write = sandbox.run("touch /input/probe")
        workspace_write = sandbox.run("echo ok > /workspace/written && cat /workspace/written")
        container = sandbox.container_id
        inspect = json.loads(
            subprocess.run(["docker", "inspect", container], capture_output=True, text=True).stdout
        )[0]["HostConfig"]
    finally:
        sandbox.destroy()
    assert identity.stdout.split() == ["65532", "65532"]
    assert not network.passed
    assert not root_write.passed and not input_write.passed
    assert workspace_write.stdout.strip() == "ok"
    assert not (workspace / "written").exists()  # writes stay inside the container
    assert inspect["NetworkMode"] == "none"
    assert inspect["ReadonlyRootfs"] is True
    assert inspect["Memory"] == 2 * 1024**3
    assert inspect["NanoCpus"] == 2 * 10**9
    assert inspect["PidsLimit"] == 256
    assert inspect["CapDrop"] == ["ALL"]
    assert container not in _containers()


def test_timeout_kills_the_command_and_removes_the_container(workspace) -> None:
    before = _containers()
    started = time.monotonic()
    result = DockerSandbox(workspace, image=IMAGE, timeout_seconds=2).run("sleep 60")
    assert result.timed_out is True
    assert time.monotonic() - started < 30
    assert _containers() == before


def test_budget_cancellation_cleans_up_the_container(workspace) -> None:
    before = _containers()
    budget = ExecutionBudget()
    sandbox = DockerSandbox(workspace, image=IMAGE, timeout_seconds=120)
    outcome = {}

    def target():
        token = current_budget.set(budget)
        try:
            outcome["result"] = sandbox.run("sleep 60")
        finally:
            current_budget.reset(token)

    thread = threading.Thread(target=target)
    thread.start()
    for _ in range(100):
        if sandbox.container_id and budget.live_processes:
            break
        time.sleep(0.1)
    budget.cancel()
    thread.join(timeout=30)
    assert outcome["result"].timed_out is True
    assert _containers() == before


def test_patch_evaluation_runs_inside_docker(tmp_path, monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    from tender.patch_eval import evaluate_patch

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("demo", root / "scripts/run_patch_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo = tmp_path / "stats-demo"
    commit = module.build_fixture(repo)
    monkeypatch.setenv("TENDER_SANDBOX_IMAGE", IMAGE)
    before = _containers()
    report = evaluate_patch(
        repo,
        commit,
        root / "examples/patch-demo/candidate-1.patch",
        root / "examples/patch-demo/criteria.toml",
        tmp_path / "out",
        sandbox="docker",
    )
    assert report["environment"]["sandbox"]["backend"] == "docker"
    assert report["outcome"]["overall"] == "rejected"
    assert report["outcome"]["regression"] == "failed"
    assert _containers() == before


@pytest.mark.asyncio
async def test_agent_workflow_verifies_in_docker(tmp_path, monkeypatch) -> None:
    from tender.artifacts import ArtifactStore
    from tender.models import BenchmarkConfig, EvaluationRun, Issue, RunStatus
    from tender.providers import ModelResponse
    from tender.services import RunService
    from tender.store import RunStore

    monkeypatch.setenv("TENDER_SANDBOX_IMAGE", IMAGE)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("return 1\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", *args], cwd=source, check=True)

    class Provider:
        async def generate(self, messages, tools=None):
            if "implementation plan" in messages[0]["content"].lower():
                return ModelResponse('{"summary":"fix","steps":["edit"],"target_files":["app.py"]}')
            return ModelResponse("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-return 1\n+return 2\n")

    check = "python3 -c \"assert open('app.py').read().strip() == 'return 2'\""
    run = EvaluationRun(
        issue=Issue(repository="o/r", title="fix", body="fix"),
        config=BenchmarkConfig(commands=[check], max_retries=0, sandbox="docker"),
    )
    before = _containers()
    finished = await RunService(
        Provider(), RunStore(tmp_path / "runs.db"), ArtifactStore(tmp_path / "artifacts")
    ).execute_workflow(run, str(source))
    assert finished.status is RunStatus.PASSED
    assert finished.outcome.independently_verified is True
    assert finished.metadata["source_unchanged"] is True
    assert _containers() == before
