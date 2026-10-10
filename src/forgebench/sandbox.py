from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
_OUTPUT_TRUNCATION_MARKER = b"\n...[ForgeBench output truncated]"


def _run_bounded(
    command: str | list[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    shell: bool = False,
    executable: str | None = None,
) -> tuple[int | None, str, str, bool]:
    """Drain command pipes while retaining only a bounded prefix of each stream."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        shell=shell,
        executable=executable,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}

    def drain(name: str, stream) -> None:
        reserve = len(_OUTPUT_TRUNCATION_MARKER)
        limit = MAX_COMMAND_OUTPUT_BYTES - reserve
        while chunk := stream.read(8192):
            remaining = limit - len(output[name])
            if remaining > 0:
                output[name].extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated[name] = True
        stream.close()

    readers = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return_code = process.wait(timeout=5)
    else:
        # A shell can exit while a background child keeps running with inherited
        # pipes. End the per-command process group when the foreground exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for reader in readers:
        reader.join(timeout=5)
    for name in output:
        if truncated[name]:
            output[name].extend(_OUTPUT_TRUNCATION_MARKER)
    return (
        return_code,
        output["stdout"].decode("utf-8", errors="replace"),
        output["stderr"].decode("utf-8", errors="replace"),
        timed_out,
    )


@dataclass(frozen=True)
class CommandResult:
    command: str
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def passed(self) -> bool:
        return self.return_code == 0 and not self.timed_out


class SandboxUnavailable(RuntimeError):
    """Raised when the requested isolation backend cannot be started."""


class SandboxProvider(Protocol):
    """Lifecycle shared by trusted local and isolated container runners."""

    def run(self, command: str) -> CommandResult: ...

    def destroy(self) -> None: ...


class Sandbox:
    """A bounded local runner. Docker is the production isolation backend."""

    def __init__(self, root: str | Path, timeout_seconds: int = 120) -> None:
        self.root = Path(root).resolve()
        self.timeout_seconds = timeout_seconds

    def run(self, command: str) -> CommandResult:
        started = time.perf_counter()
        environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root)}
        try:
            return_code, stdout, stderr, timed_out = _run_bounded(
                command,
                cwd=self.root,
                environment=environment,
                shell=True,
                executable="/bin/sh",
                timeout_seconds=self.timeout_seconds,
            )
            return CommandResult(
                command,
                return_code,
                stdout,
                stderr,
                timed_out,
                round((time.perf_counter() - started) * 1000),
            )
        except subprocess.SubprocessError as exc:
            return CommandResult(
                command,
                None,
                "",
                str(exc)[:1000],
                False,
                round((time.perf_counter() - started) * 1000),
            )

    def destroy(self) -> None:
        """Local runner has no persistent execution resource to clean up."""
        return None


class DockerSandbox:
    """Run a sequence of commands in one disposable, network-disabled container.

    The repository is mounted read-only at /input and copied once to a writable
    tmpfs at /workspace. A persistent container for one grading attempt lets
    setup commands install dependencies before test commands execute. Call
    ``destroy`` after grading; timeout and initialization failures clean up too.
    """

    def __init__(
        self,
        root: str | Path,
        image: str | None = None,
        timeout_seconds: int = 120,
        memory: str = "2g",
        cpus: str = "2",
    ) -> None:
        self.root = Path(root).resolve()
        self.image = image or os.getenv("FORGEBENCH_SANDBOX_IMAGE", "python:3.12-slim")
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.container_id: str | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def create(self) -> None:
        """Create and initialize the isolated workspace once for this attempt."""
        if self.container_id:
            return
        self._temp_dir = tempfile.TemporaryDirectory(prefix="forgebench-docker-")
        cidfile = Path(self._temp_dir.name) / "container-id"
        create_command = [
            "docker",
            "create",
            "--cidfile",
            str(cidfile),
            "--network",
            "none",
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=1g,uid=65532,gid=65532",
            "--mount",
            f"type=bind,src={self.root},dst=/input,readonly",
            self.image,
            "/bin/sh",
            "-lc",
            "while :; do sleep 3600; done",
        ]
        try:
            created = subprocess.run(create_command, capture_output=True, text=True, timeout=30)
        except FileNotFoundError as exc:
            self._cleanup_temp()
            raise SandboxUnavailable("Docker CLI is not installed") from exc
        except subprocess.SubprocessError as exc:
            self._cleanup_temp()
            raise SandboxUnavailable(f"Docker container creation failed: {exc}") from exc
        container_id = (created.stdout or "").strip()
        if created.returncode or not container_id:
            self._cleanup_temp()
            raise SandboxUnavailable(
                (created.stderr or "Docker did not return a container ID")[:1000]
            )
        self.container_id = container_id
        try:
            started = subprocess.run(
                ["docker", "start", container_id], capture_output=True, text=True, timeout=30
            )
            if started.returncode:
                raise SandboxUnavailable(
                    (started.stderr or "Docker container failed to start")[:1000]
                )
            initialized = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_id,
                    "/bin/sh",
                    "-lc",
                    "cp -R /input/. /workspace/ && chmod -R u+rwX /workspace",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if initialized.returncode:
                raise SandboxUnavailable(
                    (initialized.stderr or "Docker workspace initialization failed")[:1000]
                )
        except FileNotFoundError as exc:
            self.destroy()
            raise SandboxUnavailable("Docker CLI is not installed") from exc
        except subprocess.SubprocessError as exc:
            self.destroy()
            raise SandboxUnavailable(f"Docker container initialization failed: {exc}") from exc
        except SandboxUnavailable:
            self.destroy()
            raise

    def run(self, command: str) -> CommandResult:
        started = time.perf_counter()
        self.create()
        assert self.container_id is not None
        try:
            return_code, stdout, stderr, timed_out = _run_bounded(
                [
                    "docker",
                    "exec",
                    "--workdir",
                    "/workspace",
                    self.container_id,
                    "/bin/sh",
                    "-lc",
                    command,
                ],
                timeout_seconds=self.timeout_seconds,
            )
            if timed_out:
                self.destroy()
            return CommandResult(
                command,
                return_code,
                stdout,
                stderr,
                timed_out,
                round((time.perf_counter() - started) * 1000),
            )
        except FileNotFoundError as exc:
            self.destroy()
            raise SandboxUnavailable("Docker CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else exc.stdout or ""
            )
            stderr = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else exc.stderr or ""
            )
            self.destroy()
            return CommandResult(
                command, None, stdout, stderr, True, round((time.perf_counter() - started) * 1000)
            )
        except subprocess.SubprocessError as exc:
            self.destroy()
            raise SandboxUnavailable(f"Docker command execution failed: {exc}") from exc

    def destroy(self) -> None:
        container_id = self.container_id
        self.container_id = None
        if container_id:
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_id], capture_output=True, text=True, timeout=10
                )
            except (FileNotFoundError, subprocess.SubprocessError):
                pass
        self._cleanup_temp()

    def _cleanup_temp(self) -> None:
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None


def create_sandbox(root: str | Path, backend: str, timeout_seconds: int) -> SandboxProvider:
    """Construct the requested command runner behind one lifecycle seam."""
    if backend == "docker":
        return DockerSandbox(root, timeout_seconds=timeout_seconds)
    if backend == "local":
        return Sandbox(root, timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported sandbox backend: {backend}")
