"""OpenAI models via the Responses API, with the output constrained to a strict JSON schema.

As with Anthropic, we call `responses.create` and validate the text ourselves instead of
using `responses.parse`: `parse` raises on output that doesn't fit the schema (a
response cut off at `max_output_tokens` always doesn't) and the exception carries
neither the text nor the usage. The strict schema comes from the SDK's own
`type_to_response_format_param`, the converter `parse` uses, so the request matches.

`store=False`: the response is not kept on OpenAI's side for later retrieval; we never
use `previous_response_id`, and our own copy lives in `llm_runs`.

Usage keeps OpenAI's convention (`input_tokens` includes cached tokens); see
`llm.base` and `llm.pricing`.
"""

import time
from typing import Any, Literal, get_args

import openai
from openai.lib import type_to_response_format_param
from openai.types.responses import (
    Response,
    ResponseFormatTextJSONSchemaConfigParam,
    ResponseInputParam,
)
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, ValidationError

from jobradar.llm.base import LLMCall, LLMResult, Outcome, describe_validation_error
from jobradar.llm.pricing import cost_usd

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
# Maps the plain string from prompt frontmatter onto the SDK's Literal type.
_EFFORTS: dict[str, Effort] = {effort: effort for effort in get_args(Effort)}

# Same transient set the SDK retries itself.
_RETRYABLE_STATUSES = frozenset({408, 409, 429})

# `status == "failed"` arrives as a normal 200 response with an `error` object, so the
# SDK did not retry it. These codes are transient; the policy codes are the model (or
# its safety layer) declining, which is a refusal, not an outage.
_RETRYABLE_FAILURE_CODES = frozenset(
    {"server_error", "rate_limit_exceeded", "vector_store_timeout"}
)
_REFUSAL_FAILURE_CODES = frozenset({"bio_policy", "misalignment_policy_violation"})


class OpenAIProvider:
    name = "openai"

    def __init__(self, client: openai.OpenAI | None = None) -> None:
        # Tests inject a fake; the registry injects a client built from the environment.
        self._client = client if client is not None else openai.OpenAI(max_retries=2)

    def complete[T: BaseModel](self, call: LLMCall, schema: type[T]) -> LLMResult[T]:
        fmt = _text_format(schema)
        messages: ResponseInputParam = [
            # System first and the per-call content last: OpenAI caches the longest
            # repeated prefix automatically, so the stable part must lead.
            {"role": "system", "content": call.system},
            {"role": "user", "content": call.user},
        ]
        extra: dict[str, Any] = {}
        if call.effort is not None:
            reasoning: Reasoning = {"effort": _effort(call.effort)}
            extra["reasoning"] = reasoning

        started = time.monotonic()
        try:
            response = self._client.responses.create(
                model=call.model,
                input=messages,
                max_output_tokens=call.max_tokens,
                text={"format": fmt},
                store=False,
                **extra,
            )
        except openai.APIStatusError as e:
            return self._error(
                call,
                started,
                f"HTTP {e.status_code}: {e.message}",
                retryable=e.status_code in _RETRYABLE_STATUSES or e.status_code >= 500,
            )
        except openai.APIConnectionError as e:  # includes APITimeoutError
            return self._error(call, started, f"{type(e).__name__}: {e}", retryable=True)
        except openai.OpenAIError as e:  # e.g. a malformed response body
            return self._error(call, started, f"{type(e).__name__}: {e}", retryable=False)
        return self._result(call, schema, response, started)

    def _result[T: BaseModel](
        self, call: LLMCall, schema: type[T], response: Response, started: float
    ) -> LLMResult[T]:
        texts: list[str] = []
        refusals: list[str] = []
        for item in response.output:
            # Only final-answer messages hold the structured result; "commentary" is
            # intermediate text (the SDK's own parser applies the same rule).
            if item.type != "message" or item.phase not in (None, "final_answer"):
                continue
            for part in item.content:
                if part.type == "output_text":
                    texts.append(part.text)
                elif part.type == "refusal":
                    refusals.append(part.refusal)
        raw_text = "".join(texts) if texts else None

        parsed: T | None = None
        outcome: Outcome
        error: str | None = None
        retryable = False
        incomplete = response.incomplete_details.reason if response.incomplete_details else None
        if response.status == "failed":
            code = response.error.code if response.error else None
            message = response.error.message if response.error else "no error details"
            error = f"response failed ({code}): {message}"
            if code in _REFUSAL_FAILURE_CODES:
                outcome = "refused"
            else:
                outcome = "api_error"
                retryable = code in _RETRYABLE_FAILURE_CODES
        elif refusals:
            outcome = "refused"
            error = "refusal: " + " ".join(refusals)
        elif response.status == "incomplete" and incomplete == "content_filter":
            outcome = "refused"
            error = "incomplete: content_filter"
        elif response.status == "incomplete":
            # max_output_tokens (reasoning counts against it) or another stop reason;
            # either way the JSON is unfinished.
            outcome = "truncated"
            error = f"incomplete: {incomplete} at max_output_tokens={call.max_tokens}"
        elif raw_text is None:
            outcome = "invalid"
            error = f"no output text in response (status={response.status})"
        else:
            try:
                parsed = schema.model_validate_json(raw_text)
                outcome = "ok"
            except ValidationError as e:
                outcome = "invalid"
                error = describe_validation_error(e)

        usage = response.usage
        input_tokens = usage.input_tokens if usage else 0
        output_tokens = usage.output_tokens if usage else 0  # includes reasoning tokens
        # Verified against openai 3.57 ResponseUsage.input_tokens_details: both counts
        # are part of input_tokens, not in addition to it.
        cache_read = usage.input_tokens_details.cached_tokens if usage else 0
        cache_write = usage.input_tokens_details.cache_write_tokens if usage else 0
        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            outcome=outcome,
            error=error,
            retryable=retryable,
            provider=self.name,
            model=call.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            latency_ms=_elapsed_ms(started),
            cost_usd=cost_usd(call.model, input_tokens, output_tokens, cache_read, cache_write),
        )

    def _error(
        self, call: LLMCall, started: float, error: str, *, retryable: bool
    ) -> LLMResult[Any]:
        # parsed is None, so the result fits any schema type. A failed request bills
        # no tokens; the cost is a real 0.0 for a known model.
        return LLMResult(
            parsed=None,
            raw_text=None,
            outcome="api_error",
            error=error,
            retryable=retryable,
            provider=self.name,
            model=call.model,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            latency_ms=_elapsed_ms(started),
            cost_usd=cost_usd(call.model, 0, 0, 0, 0),
        )


def _text_format(schema: type[BaseModel]) -> ResponseFormatTextJSONSchemaConfigParam:
    # The SDK converter targets Chat Completions' shape ({"json_schema": {...}}); the
    # Responses API wants the same fields one level up.
    converted = type_to_response_format_param(schema)
    if not isinstance(converted, dict) or converted["type"] != "json_schema":
        raise TypeError(f"cannot build a JSON schema format for {schema!r}")
    spec = converted["json_schema"]
    return {
        "type": "json_schema",
        "name": spec["name"],
        "schema": spec.get("schema", {}),
        "strict": True,
    }


def _effort(value: str) -> Effort:
    # A typo in prompt frontmatter would otherwise surface as a 400 on every call.
    try:
        return _EFFORTS[value]
    except KeyError:
        raise ValueError(
            f"unknown OpenAI reasoning effort {value!r}; expected one of {sorted(_EFFORTS)}"
        ) from None


def _elapsed_ms(started: float) -> int:
    # Includes the SDK's own retry waits: this is how long the caller was blocked.
    return round((time.monotonic() - started) * 1000)
