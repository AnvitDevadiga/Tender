from __future__ import annotations

from collections import Counter

from .workflow import WorkflowResult


def failure_counts(results: list[WorkflowResult]) -> dict[str, int]:
    counts = Counter(result.failure_category.value for result in results if not result.passed)
    return dict(sorted(counts.items()))
