"""AnthropicProvider against a fake client that returns real SDK `Message` objects."""

import json
from typing import Any, cast

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel

from jobradar.llm.anthropic_provider import AnthropicProvider
from jobradar.llm.base import LLMCall

MODEL = "claude-haiku-5-5"


class Answer(BaseModel):
    label: str
    score: int


def _call(**overrides: Any) -> LLMCall:
    fields: dict[str, Any] = {
        "purpose": "extract_posting",
        "prompt_name": "extract_posting",
        "prompt_version": 1,
        "provider": "anthropic",
        "model": MODEL,
        "system": "SYSTEM PROMPT",
        "user": "USER PROMPT",
        "max_tokens": 1000,
        "effort": "low",
    }
    fields.update(overrides)
    return LLMCall(**fields)


def _message(
    text: str | None,
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> Message:
    # Haiku 5.5 thinks adaptively, so a real response can lead with a thinking block.
    content: list[dict[str, Any]] = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": usage
            or {
                "input_tokens": 120,
                "output_tokens": 40,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 0,
            },
        }
    )


class _FakeMessages:
    def __init__(self, result: Message | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeClient:
    def __init__(self, result: Message | Exception) -> None:
        self.messages = _FakeMessages(result)


def _provider(result: Message | Exception) -> tuple[AnthropicProvider, _FakeMessages]:
    fake = _FakeClient(result)
    return AnthropicProvider(client=cast(anthropic.Anthropic, fake)), fake.messages


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return anthropic.APIStatusError(f"status {status}", response=response, body=None)


GOOD = json.dumps({"label": "platform", "score": 3})


def test_ok_parses_and_reports_usage_and_cost() -> None:
    provider, _ = _provider(_message(GOOD))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "ok"
    assert result.parsed == Answer(label="platform", score=3)
    assert result.raw_text == GOOD
    assert result.error is None
    assert (result.provider, result.model) == ("anthropic", MODEL)
    assert result.input_tokens == 120
    assert result.output_tokens == 40
    assert result.cache_read_tokens == 3000
    assert result.cache_write_tokens == 0
    assert result.cost_usd == pytest.approx((120 * 0.10 + 40 * 0.50 + 3000 * 0.01) / 1e6)
    assert result.latency_ms >= 0


def test_request_shape() -> None:
    provider, fake = _provider(_message(GOOD))
    provider.complete(_call(), Answer)

    kwargs = fake.calls[0]
    assert kwargs["model"] == MODEL
    assert kwargs["max_tokens"] == 1000
    assert kwargs["messages"] == [{"role": "user", "content": "USER PROMPT"}]
    # The system prompt (which carries the taxonomy) is marked for prompt caching.
    assert kwargs["system"] == [
        {"type": "text", "text": "SYSTEM PROMPT", "cache_control": {"type": "ephemeral"}}
    ]
    config = kwargs["output_config"]
    assert config["effort"] == "low"
    assert config["format"]["type"] == "json_schema"
    schema = config["format"]["schema"]
    assert set(schema["properties"]) == {"label", "score"}
    assert schema["additionalProperties"] is False
    # Haiku 5.5 rejects sampling parameters.
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs


def test_no_effort_leaves_provider_default() -> None:
    provider, fake = _provider(_message(GOOD))
    provider.complete(_call(effort=None), Answer)
    assert "effort" not in fake.calls[0]["output_config"]


def test_unknown_effort_is_a_programming_error() -> None:
    provider, _ = _provider(_message(GOOD))
    with pytest.raises(ValueError, match="effort"):
        provider.complete(_call(effort="extreme"), Answer)


def test_schema_invalid_output_is_invalid_and_keeps_raw_text() -> None:
    bad = json.dumps({"label": "platform", "score": "lots"})
    provider, _ = _provider(_message(bad))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "invalid"
    assert result.parsed is None
    assert result.raw_text == bad
    assert result.error is not None
    assert "score" in result.error
    assert result.cost_usd is not None  # the call still cost money


def test_invalid_error_never_echoes_input_values() -> None:
    # Sensitive (resume) outputs must not leak into error strings that get logged.
    secret = json.dumps({"label": "SECRET-RESUME-LINE", "score": "SECRET-SCORE"})
    provider, _ = _provider(_message(secret))
    result = provider.complete(_call(sensitive=True), Answer)

    assert result.outcome == "invalid"
    assert result.error is not None
    assert "SECRET" not in result.error


def test_not_json_is_invalid() -> None:
    provider, _ = _provider(_message("Sure! Here is the JSON:"))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "invalid"
    assert result.raw_text == "Sure! Here is the JSON:"


def test_no_text_block_is_invalid() -> None:
    provider, _ = _provider(_message(None))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "invalid"
    assert result.raw_text is None


@pytest.mark.parametrize("stop_reason", ["max_tokens", "model_context_window_exceeded"])
def test_truncation_keeps_partial_text(stop_reason: str) -> None:
    partial = '{"label": "plat'
    provider, _ = _provider(_message(partial, stop_reason=stop_reason))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "truncated"
    assert result.parsed is None
    assert result.raw_text == partial
    assert result.error is not None
    assert stop_reason in result.error
    assert result.output_tokens == 40


def test_refusal() -> None:
    message = _message(
        None,
        stop_reason="refusal",
        stop_details={"type": "refusal", "category": "general_harms", "explanation": "no"},
    )
    provider, _ = _provider(message)
    result = provider.complete(_call(), Answer)

    assert result.outcome == "refused"
    assert result.parsed is None
    assert result.error is not None
    assert "general_harms" in result.error
    assert not result.retryable


def test_cache_write_tokens_counted() -> None:
    usage = {
        "input_tokens": 50,
        "output_tokens": 10,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": 4000,
    }
    provider, _ = _provider(_message(GOOD, usage=usage))
    result = provider.complete(_call(), Answer)

    assert result.cache_read_tokens == 0
    assert result.cache_write_tokens == 4000
    assert result.cost_usd == pytest.approx((50 * 0.10 + 10 * 0.50 + 4000 * 0.125) / 1e6)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (529, True), (408, True), (400, False), (401, False), (404, False)],
)
def test_status_errors(status: int, retryable: bool) -> None:
    provider, _ = _provider(_status_error(status))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "api_error"
    assert result.retryable is retryable
    assert result.error is not None
    assert str(status) in result.error
    assert result.parsed is None
    assert result.raw_text is None
    assert (result.input_tokens, result.output_tokens) == (0, 0)
    assert result.cost_usd == 0.0


def test_connection_error_is_retryable() -> None:
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    provider, _ = _provider(error)
    result = provider.complete(_call(), Answer)
    assert result.outcome == "api_error"
    assert result.retryable


def test_timeout_is_retryable() -> None:
    error = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://x"))
    provider, _ = _provider(error)
    result = provider.complete(_call(), Answer)
    assert result.outcome == "api_error"
    assert result.retryable


def test_provider_name() -> None:
    provider, _ = _provider(_message(GOOD))
    assert provider.name == "anthropic"
