"""OpenAIProvider against a fake client that returns real SDK `Response` objects."""

import json
from typing import Any, cast

import httpx2
import openai
import pytest
from openai.types.responses import Response
from pydantic import BaseModel

from jobradar.llm.base import LLMCall
from jobradar.llm.openai_provider import OpenAIProvider

MODEL = "gpt-6.2-sol"


class Answer(BaseModel):
    label: str
    score: int


def _call(**overrides: Any) -> LLMCall:
    fields: dict[str, Any] = {
        "purpose": "rerank",
        "prompt_name": "rerank",
        "prompt_version": 1,
        "provider": "openai",
        "model": MODEL,
        "system": "SYSTEM PROMPT",
        "user": "USER PROMPT",
        "max_tokens": 1000,
    }
    fields.update(overrides)
    return LLMCall(**fields)


def _text(text: str) -> dict[str, Any]:
    return {"type": "output_text", "text": text, "annotations": []}


def _response(
    content: list[dict[str, Any]] | None,
    *,
    status: str = "completed",
    incomplete_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    extra_output: list[dict[str, Any]] | None = None,
) -> Response:
    output: list[dict[str, Any]] = [
        # Reasoning models emit a reasoning item before the message.
        {"type": "reasoning", "id": "rs_1", "summary": []},
        *(extra_output or []),
    ]
    if content is not None:
        output.append(
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed" if status == "completed" else "incomplete",
                "content": content,
            }
        )
    return Response.model_validate(
        {
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "model": MODEL,
            "status": status,
            "output": output,
            "incomplete_details": (
                {"reason": incomplete_reason} if incomplete_reason is not None else None
            ),
            "error": error,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "usage": usage
            or {
                "input_tokens": 5000,
                "output_tokens": 300,
                "total_tokens": 5300,
                "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 100},
            },
        }
    )


class _FakeResponses:
    def __init__(self, result: Response | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeClient:
    def __init__(self, result: Response | Exception) -> None:
        self.responses = _FakeResponses(result)


def _provider(result: Response | Exception) -> tuple[OpenAIProvider, _FakeResponses]:
    fake = _FakeClient(result)
    return OpenAIProvider(client=cast(openai.OpenAI, fake)), fake.responses


def _status_error(status: int) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx2.Response(status, request=request)
    return openai.APIStatusError(f"status {status}", response=response, body=None)


GOOD = json.dumps({"label": "platform", "score": 3})


def test_ok_parses_and_reports_usage_and_cost() -> None:
    provider, _ = _provider(_response([_text(GOOD)]))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "ok"
    assert result.parsed == Answer(label="platform", score=3)
    assert result.raw_text == GOOD
    assert (result.provider, result.model) == ("openai", MODEL)
    # OpenAI convention kept: input_tokens includes the 4000 cached ones.
    assert result.input_tokens == 5000
    assert result.cache_read_tokens == 4000
    assert result.cache_write_tokens == 0
    assert result.output_tokens == 300
    assert result.cost_usd == pytest.approx((1000 * 2.00 + 4000 * 0.10 + 300 * 10.00) / 1e6)


def test_request_shape() -> None:
    provider, fake = _provider(_response([_text(GOOD)]))
    provider.complete(_call(effort="low"), Answer)

    kwargs = fake.calls[0]
    assert kwargs["model"] == MODEL
    assert kwargs["max_output_tokens"] == 1000
    assert kwargs["input"] == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "USER PROMPT"},
    ]
    assert kwargs["reasoning"] == {"effort": "low"}
    assert kwargs["store"] is False
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["name"] == "Answer"
    assert set(fmt["schema"]["properties"]) == {"label", "score"}
    assert fmt["schema"]["additionalProperties"] is False


def test_no_effort_sends_no_reasoning() -> None:
    provider, fake = _provider(_response([_text(GOOD)]))
    provider.complete(_call(), Answer)
    assert "reasoning" not in fake.calls[0]


def test_unknown_effort_is_a_programming_error() -> None:
    provider, _ = _provider(_response([_text(GOOD)]))
    with pytest.raises(ValueError, match="effort"):
        provider.complete(_call(effort="extreme"), Answer)


def test_schema_invalid_output_is_invalid_and_keeps_raw_text() -> None:
    bad = json.dumps({"label": "SECRET", "score": "SECRET"})
    provider, _ = _provider(_response([_text(bad)]))
    result = provider.complete(_call(sensitive=True), Answer)

    assert result.outcome == "invalid"
    assert result.parsed is None
    assert result.raw_text == bad
    assert result.error is not None
    assert "score" in result.error
    assert "SECRET" not in result.error


def test_commentary_phase_text_is_not_the_answer() -> None:
    commentary = {
        "type": "message",
        "id": "msg_0",
        "role": "assistant",
        "status": "completed",
        "phase": "commentary",
        "content": [_text("thinking out loud")],
    }
    provider, _ = _provider(_response([_text(GOOD)], extra_output=[commentary]))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "ok"
    assert result.raw_text == GOOD


def test_no_message_is_invalid() -> None:
    provider, _ = _provider(_response(None))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "invalid"
    assert result.raw_text is None


def test_refusal_part() -> None:
    provider, _ = _provider(_response([{"type": "refusal", "refusal": "I can't help"}]))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "refused"
    assert result.parsed is None
    assert result.error is not None
    assert "I can't help" in result.error


def test_truncated_at_max_output_tokens() -> None:
    partial = '{"label": "pla'
    response = _response(
        [_text(partial)], status="incomplete", incomplete_reason="max_output_tokens"
    )
    provider, _ = _provider(response)
    result = provider.complete(_call(), Answer)

    assert result.outcome == "truncated"
    assert result.raw_text == partial
    assert result.error is not None
    assert "max_output_tokens" in result.error
    assert result.cost_usd is not None and result.cost_usd > 0


def test_truncated_with_no_message_at_all() -> None:
    # The whole budget can go to reasoning, leaving no message item.
    response = _response(None, status="incomplete", incomplete_reason="max_output_tokens")
    provider, _ = _provider(response)
    result = provider.complete(_call(), Answer)
    assert result.outcome == "truncated"
    assert result.raw_text is None


def test_content_filter_is_refused() -> None:
    response = _response([_text("{")], status="incomplete", incomplete_reason="content_filter")
    provider, _ = _provider(response)
    result = provider.complete(_call(), Answer)
    assert result.outcome == "refused"
    assert result.error is not None
    assert "content_filter" in result.error


def test_failed_status_is_api_error() -> None:
    error = {"code": "server_error", "message": "boom"}
    provider, _ = _provider(_response(None, status="failed", error=error))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "api_error"
    assert result.error is not None
    assert "boom" in result.error
    assert result.retryable  # server_error is transient


def test_cache_write_tokens_recorded_but_billed_as_input() -> None:
    usage = {
        "input_tokens": 3000,
        "output_tokens": 0,
        "total_tokens": 3000,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 2500},
        "output_tokens_details": {"reasoning_tokens": 0},
    }
    provider, _ = _provider(_response([_text(GOOD)], usage=usage))
    result = provider.complete(_call(), Answer)
    assert result.cache_write_tokens == 2500
    assert result.cost_usd == pytest.approx(3000 * 2.00 / 1e6)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (408, True), (400, False), (401, False)],
)
def test_status_errors(status: int, retryable: bool) -> None:
    provider, _ = _provider(_status_error(status))
    result = provider.complete(_call(), Answer)

    assert result.outcome == "api_error"
    assert result.retryable is retryable
    assert result.error is not None
    assert str(status) in result.error
    assert (result.input_tokens, result.output_tokens) == (0, 0)
    assert result.cost_usd == 0.0


def test_connection_error_is_retryable() -> None:
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://x"))
    provider, _ = _provider(error)
    result = provider.complete(_call(), Answer)
    assert result.outcome == "api_error"
    assert result.retryable


def test_provider_name() -> None:
    provider, _ = _provider(_response([_text(GOOD)]))
    assert provider.name == "openai"


def test_failed_on_policy_is_refused() -> None:
    error = {"code": "bio_policy", "message": "declined"}
    provider, _ = _provider(_response(None, status="failed", error=error))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "refused"
    assert not result.retryable


def test_failed_on_bad_prompt_is_not_retryable() -> None:
    error = {"code": "invalid_prompt", "message": "bad"}
    provider, _ = _provider(_response(None, status="failed", error=error))
    result = provider.complete(_call(), Answer)
    assert result.outcome == "api_error"
    assert not result.retryable
