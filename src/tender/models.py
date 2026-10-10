from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .state import WorkflowState


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    PASSED = "passed"
    FAILED = "failed"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class FailureCategory(StrEnum):
    NONE = "none"
    ENVIRONMENT = "environment_setup"
    MODEL_TOOL = "model_tool_failure"
    RETRIEVAL = "retrieval"
    PLANNING = "planning"
    PATCH = "patch"
    TEST = "test"
    TOOLING = "tooling"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class SandboxBackend(StrEnum):
    LOCAL = "local"
    DOCKER = "docker"


class Issue(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    repository: str
    number: int | None = None
    title: str
    body: str = ""
    base_ref: str = "main"
    base_commit: str | None = None
    source_url: str | None = None


class BenchmarkConfig(BaseModel):
    model: str = "local-deterministic"
    architecture: str = "single-agent"
    retrieval: str = "text"
    max_retries: int = Field(default=2, ge=0, le=10)
    setup_commands: list[str] = Field(default_factory=list, max_length=20)
    commands: list[str] = Field(default_factory=lambda: ["pytest -q"])
    static_analysis_commands: list[str] = Field(default_factory=list, max_length=20)
    verification_commands: list[str] = Field(default_factory=list, max_length=20)
    require_human_approval: bool = False
    sandbox: SandboxBackend = SandboxBackend.LOCAL
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)


class EvaluationRun(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    issue: Issue
    config: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    benchmark_id: str = "unassigned"
    status: RunStatus = RunStatus.QUEUED
    failure_category: FailureCategory = FailureCategory.NONE
    current_state: WorkflowState | None = None
    attempts: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int | None = None
    patch: str | None = None
    test_output: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEvent(BaseModel):
    """Append-only audit event. Events are the source for post-run inspection."""

    run_id: UUID
    name: str
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateRunRequest(BaseModel):
    issue: Issue
    config: BenchmarkConfig = Field(default_factory=BenchmarkConfig)
    workspace: str | None = None
    benchmark_id: str = "ad-hoc"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class GitHubIssueRequest(BaseModel):
    url: str = Field(min_length=1)


class HumanApprovalRequest(BaseModel):
    decision: ApprovalDecision
    note: str = Field(default="", max_length=1000)
