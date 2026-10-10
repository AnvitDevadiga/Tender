from __future__ import annotations

from dataclasses import dataclass

from .patches import PatchArtifact
from .sandbox import CommandResult, SandboxProvider


@dataclass(frozen=True)
class Grade:
    passed: bool
    results: tuple[CommandResult, ...]
    reason: str


class TestGrader:
    def grade(
        self,
        sandbox: SandboxProvider,
        commands: list[str],
        patch: PatchArtifact | None = None,
        setup_commands: list[str] | None = None,
    ) -> Grade:
        if patch and not patch.valid:
            return Grade(False, (), "protected_files_modified")
        results = []
        try:
            for command in setup_commands or []:
                result = sandbox.run(command)
                results.append(result)
                if result.timed_out:
                    return Grade(False, tuple(results), "timeout")
                if not result.passed:
                    return Grade(False, tuple(results), "environment_setup_failed")
            for command in commands:
                result = sandbox.run(command)
                results.append(result)
                if result.timed_out:
                    return Grade(False, tuple(results), "timeout")
                if not result.passed:
                    return Grade(False, tuple(results), "command_failed")
            return Grade(True, tuple(results), "all_commands_passed")
        finally:
            sandbox.destroy()
