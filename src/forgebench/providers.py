from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol
from urllib import request
from urllib.error import HTTPError


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


class GroqProvider:
    """Groq Chat Completions adapter using only the standard library.

    Set GROQ_API_KEY and optionally GROQ_MODEL. No key is required for offline tests.
    """

    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    supports_directory_listing = False

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 120,
        max_output_tokens: int | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        if self.api_key and not self.api_key.isascii():
            raise ValueError(
                "GROQ_API_KEY contains non-ASCII characters; use straight quotes and "
                "paste the key exactly"
            )

    async def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> ModelResponse:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        if tools:
            payload["tools"] = tools
        body = json.dumps(payload).encode("utf-8")
        return await asyncio.to_thread(self._request, body)

    def _request(self, body: bytes) -> ModelResponse:
        req = request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "ForgeBench/0.1 (+https://github.com/forgebench)",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Groq API rejected the request ({exc.code}): {detail[:500]}"
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
