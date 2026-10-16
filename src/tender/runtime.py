from __future__ import annotations

from .models import BenchmarkConfig
from .providers import (
    DeterministicProvider,
    GroqProvider,
    ModelProvider,
    OpenAICompatibleProvider,
)


def provider_for(config: BenchmarkConfig) -> ModelProvider:
    """Select a provider from configuration without ever storing credentials.

    ``groq/<model>`` uses GROQ_API_KEY; ``openai/<model>`` uses any
    OpenAI-compatible endpoint configured through OPENAI_API_KEY,
    OPENAI_BASE_URL, and OPENAI_MODEL. Anything else stays deterministic and
    offline so tests and validation never require a key.
    """
    prefix, _, model = config.model.partition("/")
    if prefix == "groq":
        return GroqProvider(model=model or None, max_output_tokens=config.max_output_tokens)
    if prefix == "openai":
        return OpenAICompatibleProvider(
            model=model or None, max_output_tokens=config.max_output_tokens
        )
    return DeterministicProvider()
