from __future__ import annotations

from dataclasses import dataclass

from .workflow import WorkflowResult


@dataclass(frozen=True)
class ExperimentMetrics:
    runs: int
    passed: int
    pass_rate: float
    average_attempts: float
    retry_recovery_rate: float
    average_latency_ms: float = 0.0
    total_cost_usd: float | None = None
    total_tool_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


def summarize(results: list[WorkflowResult]) -> ExperimentMetrics:
    runs = len(results)
    passed = sum(result.passed for result in results)
    recovered = sum(result.passed and len(result.attempts) > 1 for result in results)
    initial_failures = sum(len(result.attempts) > 1 for result in results)
    return ExperimentMetrics(
        runs=runs,
        passed=passed,
        pass_rate=passed / runs if runs else 0.0,
        average_attempts=sum(len(result.attempts) for result in results) / runs if runs else 0.0,
        retry_recovery_rate=recovered / initial_failures if initial_failures else 0.0,
        average_latency_ms=sum(result.latency_ms for result in results) / runs if runs else 0.0,
        total_cost_usd=(
            sum(result.cost_usd for result in results if result.cost_usd is not None)
            if any(result.cost_usd is not None for result in results)
            else None
        ),
        total_tool_calls=sum(result.tool_calls for result in results),
        total_input_tokens=sum(result.input_tokens for result in results),
        total_output_tokens=sum(result.output_tokens for result in results),
    )
