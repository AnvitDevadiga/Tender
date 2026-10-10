import pytest

from tender.experiments import ExperimentManifest
from tender.failures import failure_counts
from tender.metrics import summarize


def test_manifest_and_metrics() -> None:
    manifest = ExperimentManifest.load("experiments/retry-ablation-v1.toml")
    assert manifest.retry_budgets == (0, 1, 2)
    measured = ExperimentManifest.load("experiments/groq-retry-ablation-3tasks-v1.toml")
    assert measured.repetitions == 2
    assert measured.input_usd_per_million_tokens == 0.075
    assert measured.output_usd_per_million_tokens == 0.30
    metrics = summarize([])
    assert metrics.pass_rate == 0.0
    assert failure_counts([]) == {}


def test_retrieval_ablation_manifest_and_config_bounds() -> None:
    from pydantic import ValidationError

    from tender.models import BenchmarkConfig

    manifest = ExperimentManifest.load("experiments/retrieval-cap-ab-v1.toml")
    assert manifest.retrieval_limits == (12, 6)
    assert manifest.retry_budgets == (0,)
    assert BenchmarkConfig(retrieval_limit=6).retrieval_limit == 6
    with pytest.raises(ValidationError):
        BenchmarkConfig(retrieval_limit=0)
