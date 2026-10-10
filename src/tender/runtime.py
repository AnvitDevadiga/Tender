from __future__ import annotations

from .models import BenchmarkConfig
from .providers import DeterministicProvider, GroqProvider, ModelProvider


def provider_for(config: BenchmarkConfig) -> ModelProvider:
    """Select a provider from configuration without ever storing credentials."""
    if config.model.startswith("groq/"):
        model = config.model.split("/", 1)[1] or None
        return GroqProvider(model=model, max_output_tokens=config.max_output_tokens)
    return DeterministicProvider()
