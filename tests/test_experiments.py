from forgebench.experiments import ExperimentManifest
from forgebench.failures import failure_counts
from forgebench.metrics import summarize


def test_manifest_and_metrics() -> None:
    manifest = ExperimentManifest.load("experiments/retry-ablation-v1.toml")
    assert manifest.retry_budgets == (0, 1, 2)
    metrics = summarize([])
    assert metrics.pass_rate == 0.0
    assert failure_counts([]) == {}
