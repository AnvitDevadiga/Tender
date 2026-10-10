from tender.models import BenchmarkConfig
from tender.providers import DeterministicProvider, GroqProvider
from tender.runtime import provider_for


def test_runtime_defaults_to_offline_provider() -> None:
    assert isinstance(provider_for(BenchmarkConfig()), DeterministicProvider)


def test_runtime_selects_groq_without_reading_key_into_config() -> None:
    provider = provider_for(BenchmarkConfig(model="groq/openai/gpt-oss-20b"))
    assert isinstance(provider, GroqProvider)
    assert provider.model == "openai/gpt-oss-20b"
