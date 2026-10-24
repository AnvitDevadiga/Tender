from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .evidence import EvidenceWriter, environment_identity, sanitize
from .models import EvaluationRun, RunEvent
from .workflow import WorkflowResult


class ArtifactConflict(OSError):
    """A run's artifact directory already exists and is never overwritten."""


class ArtifactStore:
    """Write an auditable, portable directory for each completed workflow.

    Layout of ``<root>/<run_id>/``:

    * ``evidence/`` – immutable, hash-sealed source evidence and derived summary
      (see ``tender.evidence``). Never modified after publication.
    * ``run.json``, ``result.json``, ``attempt-N/`` – convenience summaries kept
      for compatibility. Approval and benchmark verification update these and
      add ``approval.json`` / ``verification.json``; ``evidence/`` stays as
      originally published.

    Artifacts are first written to ``<root>/.staging/`` and then published with
    one directory rename, inside the run store's lease-fenced finalization.
    """

    def __init__(self, root: str | Path = "artifacts") -> None:
        self.root = Path(root)

    def path_for(self, run: EvaluationRun) -> Path:
        return self.root / str(run.id)

    # -- staged publication ---------------------------------------------

    def stage(
        self,
        run: EvaluationRun,
        result: WorkflowResult,
        *,
        events: list[RunEvent] | None = None,
        repository: dict[str, Any] | None = None,
    ) -> Path:
        staging = self.root / ".staging" / f"{run.id}-{uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            self._write_summaries(staging, run, result)
            self._write_evidence(staging / "evidence", run, result, events or [], repository)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return staging

    def publish(self, staging: Path, run: EvaluationRun) -> Path:
        target = self.path_for(run)
        if target.exists():
            raise ArtifactConflict(f"artifacts already published for run {run.id}")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        return target

    @staticmethod
    def discard(staging: Path | None) -> None:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    def write(self, run: EvaluationRun, result: WorkflowResult) -> Path:
        """Stage and publish without lease fencing (single-process callers)."""
        staging = self.stage(run, result)
        try:
            return self.publish(staging, run)
        except BaseException:
            self.discard(staging)
            raise

    # -- summaries ----------------------------------------------------------

    def _write_summaries(self, target: Path, run: EvaluationRun, result: WorkflowResult) -> None:
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

    def _write_evidence(
        self,
        directory: Path,
        run: EvaluationRun,
        result: WorkflowResult,
        events: list[RunEvent],
        repository: dict[str, Any] | None,
    ) -> None:
        bundle = EvidenceWriter(directory)
        bundle.add_json(
            "source/task.json",
            {
                "run_id": str(run.id),
                "benchmark_id": run.benchmark_id,
                "issue": run.issue.model_dump(mode="json"),
            },
            kind="source",
        )
        bundle.add_json("source/config.json", run.config.model_dump(mode="json"), kind="source")
        bundle.add_json(
            "source/repository.json",
            {
                "requested_base_commit": run.metadata.get("requested_base_commit"),
                "executed_base_commit": run.metadata.get("executed_base_commit")
                or run.metadata.get("base_commit"),
                "verification_commit": result.base_commit,
                "source": repository or run.metadata.get("source_repository"),
                "source_unchanged": run.metadata.get("source_unchanged"),
            },
            kind="source",
        )
        bundle.add_json(
            "source/environment.json",
            environment_identity(run.config.sandbox.value),
            kind="source",
        )
        bundle.add_json(
            "source/model.json",
            {
                "model": run.config.model,
                "provider": run.config.model.partition("/")[0]
                if "/" in run.config.model
                else "local",
                "max_output_tokens": run.config.max_output_tokens,
            },
            kind="source",
        )
        bundle.add_jsonl(
            "source/events.jsonl",
            [event.model_dump(mode="json") for event in events],
            kind="source",
        )
        attempt_index = []
        for attempt in result.attempts:
            prefix = f"source/attempts/{attempt.number}"
            patch_digest = bundle.add_raw_text(
                f"{prefix}/patch.diff", attempt.patch.diff, kind="source"
            )
            bundle.add_json(
                f"{prefix}/gates.json",
                [
                    {
                        "name": gate.name,
                        "passed": gate.grade.passed,
                        "reason": gate.grade.reason,
                        "results": [asdict(item) for item in gate.grade.results],
                    }
                    for gate in attempt.quality_gates
                ],
                kind="source",
            )
            bundle.add_json(
                f"{prefix}/verification.json", list(attempt.verification), kind="source"
            )
            attempt_index.append(
                {
                    "attempt": attempt.number,
                    "patch_sha256": attempt.patch.sha256,
                    "patch_file_sha256": patch_digest,
                    "changed_files": list(attempt.patch.changed_files),
                    "reason": attempt.grade.reason,
                }
            )
        bundle.add_json(
            "derived/summary.json",
            {
                "status": run.status.value,
                "failure_category": run.failure_category.value,
                "outcome": result.outcome.model_dump(mode="json") if result.outcome else None,
                "attempts": attempt_index,
                "timing_ms": run.metadata.get("timing_ms", result.timing_ms),
                "counters": result.counters,
                "tokens": {"input": result.input_tokens, "output": result.output_tokens},
                "cost": run.metadata.get("cost"),
                "note": "Derived from the source files in this bundle.",
            },
            kind="derived",
        )
        bundle.finish(
            {
                "run_id": str(run.id),
                "generator": "tender.artifacts.ArtifactStore",
                "final_patch_sha256": result.attempts[-1].patch.sha256 if result.attempts else None,
                "events_recorded": len(events),
            }
        )

    # -- post-publication summaries (evidence/ is never modified) -----------

    def record_verification(self, run: EvaluationRun, checks: list[dict[str, object]]) -> None:
        """Record the benchmark verifier's final outcome next to the sealed evidence."""
        target = self.path_for(run)
        if not target.is_dir():
            return
        verification = {
            "passed": run.status.value == "passed",
            "failure_category": run.failure_category.value,
            "checks": sanitize(checks),
            "stage": "independent_final_verification",
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
            "outcome": result.outcome.model_dump(mode="json") if result.outcome else None,
            "timing_ms": result.timing_ms,
            "counters": result.counters,
        }
