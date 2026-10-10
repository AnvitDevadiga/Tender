from forgebench.experiments import ExperimentManifest
from forgebench.failures import failure_counts
from forgebench.metrics import summarize


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
