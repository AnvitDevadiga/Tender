"""Separate evaluation outcomes for new runs.

Earlier records folded several properties into ``status``/``passed`` and a
``regression_free`` flag that only meant "the independent target check passed".
New runs record each property separately; historical records keep their
original fields and are interpreted through the dated erratum in
docs/EVALUATION_ERRATA.md.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OUTCOME_SCHEMA = "tender.outcome.v1"


class CheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    NOT_CONFIGURED = "not_configured"
    ERROR = "error"
    UNKNOWN = "unknown"


class OverallStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"
    INCONCLUSIVE = "inconclusive"
    PENDING_APPROVAL = "pending_approval"


IntegrityState = Literal["passed", "violated", "not_checked"]
EnvironmentState = Literal["ok", "setup_failed", "sandbox_unavailable", "error", "unknown"]
ProviderState = Literal["ok", "failed", "not_used", "unknown"]
PolicyState = Literal["ok", "rejected"]


class EvaluationOutcome(BaseModel):
    """Distinct properties of one evaluation; ``overall`` is derived from them."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["tender.outcome.v1"] = OUTCOME_SCHEMA
    target: CheckOutcome = CheckOutcome.UNKNOWN
    regression: CheckOutcome = CheckOutcome.UNKNOWN
    integrity: IntegrityState = "not_checked"
    environment: EnvironmentState = "unknown"
    provider: ProviderState = "unknown"
    timed_out: bool = False
    policy: PolicyState = "ok"
    independently_verified: bool = False
    verification_commit: str | None = None
    #: Agent-side stage that stopped the run before independent verification
    #: (planning, patch, agent_tests, static_analysis, diff_review); None otherwise.
    failed_stage: str | None = None
    overall: OverallStatus = OverallStatus.INCONCLUSIVE
    reasons: list[str] = Field(default_factory=list, max_length=50)

    def finalize(self, *, awaiting_approval: bool = False) -> EvaluationOutcome:
        self.overall = derive_overall(self, awaiting_approval=awaiting_approval)
        return self


def derive_overall(outcome: EvaluationOutcome, *, awaiting_approval: bool = False) -> OverallStatus:
    """Accept only when every required property was independently established."""
    if outcome.policy == "rejected" or outcome.integrity == "violated":
        return OverallStatus.REJECTED
    if outcome.environment in {"setup_failed", "sandbox_unavailable", "error"}:
        return OverallStatus.ERROR
    if outcome.provider == "failed":
        return OverallStatus.ERROR
    if outcome.target is CheckOutcome.FAILED or outcome.regression is CheckOutcome.FAILED:
        return OverallStatus.REJECTED
    if outcome.failed_stage and not outcome.timed_out:
        return OverallStatus.REJECTED
    if outcome.timed_out:
        return OverallStatus.INCONCLUSIVE
    if (
        outcome.target is CheckOutcome.PASSED
        and outcome.regression in {CheckOutcome.PASSED, CheckOutcome.NOT_CONFIGURED}
        and outcome.integrity == "passed"
        and outcome.independently_verified
    ):
        return OverallStatus.PENDING_APPROVAL if awaiting_approval else OverallStatus.ACCEPTED
    if outcome.target is CheckOutcome.ERROR or outcome.regression is CheckOutcome.ERROR:
        return OverallStatus.ERROR
    return OverallStatus.INCONCLUSIVE
