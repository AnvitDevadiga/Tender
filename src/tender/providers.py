from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import request
from urllib.error import HTTPError

from . import net


@dataclass(frozen=True)
class ModelResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


class ModelProvider(Protocol):
    """Provider boundary. Network-backed adapters belong outside the domain layer."""

    async def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse: ...


class DeterministicProvider:
    """Offline provider for tests and local plumbing; it never claims agent success."""

    async def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse:
        return ModelResponse(text="No model provider configured.")


class ChatCompletionsProvider:
    """OpenAI-compatible Chat Completions adapter using only the standard library.

    Subclasses fix the endpoint and credential source. No key is required for
    offline tests; a missing key fails loudly at generation time.
    """

    tool_allowlist = frozenset({"search_code", "read_file", "write_file", "replace_text"})
    #: OpenAI's reasoning models renamed the output cap field.
    max_tokens_field = "max_tokens"
    #: Reasoning-style endpoints reject an explicit temperature override.
    supports_temperature = True

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        endpoint: str,
        api_key_name: str,
        api_name: str | None = None,
        timeout: int = 120,
        max_output_tokens: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.api_key_name = api_key_name
        self.api_name = api_name or api_key_name
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        if self.api_key and not self.api_key.isascii():
            raise ValueError(
                f"{api_key_name} contains non-ASCII characters; use straight quotes and "
                "paste the key exactly"
            )

    async def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError(f"{self.api_key_name} is not configured")
        try:
            net.require_secure_endpoint(self.endpoint)
        except ValueError as exc:
            raise RuntimeError(f"{self.api_name} is not configured securely: {exc}") from exc
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.supports_temperature:
            payload["temperature"] = 0
        if self.max_output_tokens is not None:
            payload[self.max_tokens_field] = self.max_output_tokens
        payload.update(self._payload_extras(messages))
        if tools:
            payload["tools"] = tools
        try:
            return await asyncio.to_thread(self._request, json.dumps(payload).encode("utf-8"))
        except RuntimeError as exc:
            # A model that rejects the explicit temperature (a reasoning family
            # outside the prefix heuristic) is retried once without it.
            if "temperature" not in payload or "temperature" not in str(exc).lower():
                raise
            payload.pop("temperature")
            self.supports_temperature = False
            return await asyncio.to_thread(self._request, json.dumps(payload).encode("utf-8"))

    def _payload_extras(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Provider-specific request options; JSON mode is shared by compatible APIs."""
        if self._expects_json_object(messages):
            return {"response_format": {"type": "json_object"}}
        return {}

    @staticmethod
    def _expects_json_object(messages: list[dict[str, Any]]) -> bool:
        return any(
            message.get("role") == "system"
            and "Return only a JSON object" in str(message.get("content", ""))
            for message in messages
        )

    def _request(self, body: bytes) -> ModelResponse:
        req = request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Tender/0.1 (+https://github.com/AnvitDevadiga/tender)",
            },
        )
        try:
            with net.open_url(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except net.RedirectRefused as exc:
            raise RuntimeError(f"{self.api_name} API rejected the request ({exc.code}): {exc.msg}")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{self.api_name} API rejected the request ({exc.code}): {detail[:500]}"
            ) from exc
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        message = choices[0].get("message") or {} if choices else {}
        content = message.get("content")
        text = content if isinstance(content, str) else ""
        calls = tuple(
            ToolCall(
                id=str(call.get("id", "")),
                name=str((call.get("function") or {}).get("name", "")),
                arguments=str((call.get("function") or {}).get("arguments", "{}")),
            )
            for call in message.get("tool_calls", [])
        )
        return ModelResponse(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            tool_calls=calls,
        )


class GroqProvider(ChatCompletionsProvider):
    """Groq chat completions adapter. Set GROQ_API_KEY and optionally GROQ_MODEL."""

    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_output_tokens: int | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key or os.getenv("GROQ_API_KEY"),
            model=model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            endpoint=self.endpoint,
            api_key_name="GROQ_API_KEY",
            api_name="Groq",
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )

    def _payload_extras(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        extras = super()._payload_extras(messages)
        if extras:
            # Groq hides chain-of-thought fields when asked for bare JSON objects.
            extras["reasoning_format"] = "hidden"
        return extras


class OpenAICompatibleProvider(ChatCompletionsProvider):
    """Any OpenAI-compatible chat completions API, including self-hosted servers.

    Set OPENAI_API_KEY and optionally OPENAI_BASE_URL and OPENAI_MODEL. For
    local servers (Ollama, vLLM, LM Studio) point OPENAI_BASE_URL at the local
    endpoint; the key may then be any placeholder value.
    """

    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_output_tokens: int | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_base = (base_url or os.getenv("OPENAI_BASE_URL") or self.default_base_url).rstrip(
            "/"
        )
        super().__init__(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            model=model or os.getenv("OPENAI_MODEL", self.default_model),
            endpoint=f"{resolved_base}/chat/completions",
            api_key_name="OPENAI_API_KEY",
            api_name="OpenAI-compatible",
            timeout=timeout,
            max_output_tokens=max_output_tokens,
        )
        if self.model.startswith(("o1", "o3", "o4", "gpt-5")):
            # Reasoning families renamed the output cap and reject temperature overrides.
            self.max_tokens_field = "max_completion_tokens"
            self.supports_temperature = False
