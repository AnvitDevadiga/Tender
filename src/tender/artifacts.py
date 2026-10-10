from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import EvaluationRun
from .workflow import WorkflowResult


class ArtifactStore:
    """Write an auditable, portable directory for each completed workflow."""

    def __init__(self, root: str | Path = "artifacts") -> None:
        self.root = Path(root)

    def path_for(self, run: EvaluationRun) -> Path:
        return self.root / str(run.id)

    def write(self, run: EvaluationRun, result: WorkflowResult) -> Path:
        target = self.path_for(run)
        target.mkdir(parents=True, exist_ok=True)
        (target / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
        (target / "result.json").write_text(
            json.dumps(self._result(result), indent=2), encoding="utf-8"
        )
        for attempt in result.attempts:
            attempt_dir = target / f"attempt-{attempt.number}"
            attempt_dir.mkdir()
            (attempt_dir / "patch.diff").write_text(attempt.patch.diff, encoding="utf-8")
            (attempt_dir / "quality_gates.json").write_text(
                json.dumps(
                    [
                        {
                            "name": gate.name,
                            "passed": gate.grade.passed,
                            "reason": gate.grade.reason,
                            "results": [asdict(item) for item in gate.grade.results],
                        }
                        for gate in attempt.quality_gates
                    ],
                    indent=2,
                ),
                encoding="utf-8",
            )
            (attempt_dir / "grade.json").write_text(
                json.dumps(
                    {
                        "passed": attempt.grade.passed,
                        "reason": attempt.grade.reason,
                        "results": [asdict(item) for item in attempt.grade.results],
                        "changed_files": attempt.patch.changed_files,
                        "patch_sha256": attempt.patch.sha256,
                        "protected_file_changes": attempt.patch.protected_file_changes,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return target

    def record_verification(self, run: EvaluationRun, checks: list[dict[str, object]]) -> None:
        """Update portable artifacts with the benchmark verifier's final outcome."""
        target = self.path_for(run)
        if not target.is_dir():
            return
        verification = {
            "passed": run.status.value == "passed",
            "failure_category": run.failure_category.value,
            "checks": checks,
        }
        (target / "verification.json").write_text(
            json.dumps(verification, indent=2), encoding="utf-8"
        )
        (target / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
        result_path = target / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["passed"] = verification["passed"]
            result["failure_category"] = run.failure_category.value
            result["verification"] = verification
            result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    def record_approval(self, run: EvaluationRun, decision: str, note: str) -> None:
        """Reflect the authenticated approval outcome in an existing run artifact."""
        target = self.path_for(run)
        if not target.is_dir():
            return
        approval = {
            "decision": decision,
            "note": note,
            "status": run.status.value,
            "current_state": run.current_state.value if run.current_state else None,
        }
        (target / "approval.json").write_text(json.dumps(approval, indent=2), encoding="utf-8")
        (target / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
        result_path = target / "result.json"
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["passed"] = run.status.value == "passed"
            result["awaiting_approval"] = False
            result["approval"] = approval
            result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    @staticmethod
    def _result(result: WorkflowResult) -> dict[str, object]:
        return {
            "passed": result.passed,
            "awaiting_approval": result.awaiting_approval,
            "failure_category": result.failure_category.value,
            "attempts": len(result.attempts),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "tool_calls": result.tool_calls,
        }
