import json

import pytest

from tender.providers import GroqProvider, ModelResponse


def test_groq_request_has_explicit_user_agent(monkeypatch) -> None:
    provider = GroqProvider(api_key="gsk_test")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"ok"}}],"usage":{}}'

    def fake_urlopen(req, timeout):
        captured.update(dict(req.header_items()))
        return Response()

    monkeypatch.setattr("tender.net.open_url", fake_urlopen)
    result = provider._request(b'{"model":"openai/gpt-oss-20b"}')
    assert result.text == "ok"
    assert captured["User-agent"].startswith("Tender/")


def test_groq_response_preserves_structured_tool_calls(monkeypatch) -> None:
    provider = GroqProvider(api_key="gsk_test")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                b'{"choices":[{"message":{"content":null,"tool_calls":[{"id":"call-1",'
                b'"function":{"name":"read_file","arguments":"{\\"path\\":\\"app.py\\"}"}}]}}],'
                b'"usage":{"prompt_tokens":3,"completion_tokens":2}}'
            )

    monkeypatch.setattr("tender.net.open_url", lambda req, timeout: Response())

    response = provider._request(b'{"model":"openai/gpt-oss-20b"}')

    assert response.text == ""
    assert response.input_tokens == 3
    assert response.output_tokens == 2
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == '{"path":"app.py"}'


@pytest.mark.asyncio
async def test_groq_enables_json_mode_for_json_only_system_prompts(monkeypatch) -> None:
    provider = GroqProvider(api_key="gsk_test")
    captured = {}

    def fake_request(body):
        captured.update(json.loads(body))
        return ModelResponse('{"summary":"fix"}')

    monkeypatch.setattr(provider, "_request", fake_request)
    await provider.generate(
        [
            {
                "role": "system",
                "content": 'Return only a JSON object with keys "summary" and "steps".',
            },
            {"role": "user", "content": "Plan the change."},
        ]
    )

    assert captured["response_format"] == {"type": "json_object"}
    assert captured["reasoning_format"] == "hidden"
