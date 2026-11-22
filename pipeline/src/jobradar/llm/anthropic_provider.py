"""Claude via the Anthropic Messages API, with the output constrained to a JSON schema.

We call `messages.create` with `output_config.format` rather than the SDK's
`messages.parse` helper. `parse` validates inside the SDK and raises on any text that
doesn't fit the model, including a response cut off by `max_tokens`; the exception
carries neither the raw text nor the usage, so we would lose what the repair retry
needs and the cost of a call we paid for. Validating ourselves keeps both. The schema
is built with the SDK's own `transform_schema`, the same one `parse` uses, so the
request is identical.

Retries: the client's built-in retries (2, exponential backoff, honoring Retry-After)
cover 408/409/429/5xx and connection errors. An error that survives them is returned as
`api_error`, with `retryable` telling the job queue whether to try again later; we add
no retry layer of our own here.
"""

import time
from typing import Any, Literal, get_args

import anthropic
from anthropic.types import (
    Message,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
)
from pydantic import BaseModel, ValidationError

from jobradar.llm.base import LLMCall, LLMResult, Outcome, describe_validation_error
from jobradar.llm.pricing import cost_usd

Effort = Literal["low", "medium", "high", "xhigh", "max"]
# Maps the plain string from prompt frontmatter onto the SDK's Literal type.
_EFFORTS: dict[str, Effort] = {effort: effort for effort in get_args(Effort)}

# The statuses the SDK itself treats as transient. Anything else (400 bad request,
# 401/403 key problems, 404 unknown model) fails the same way every time.
_RETRYABLE_STATUSES = frozenset({408, 409, 429})


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # Tests inject a fake; the registry injects a client built from the environment.
        # Built here only as a fallback, so importing this module never needs a key.
        self._client = (
            client
            if client is not None
            else anthropic.Anthropic(max_retries=2, open_telemetry=False)
        )

    def complete[T: BaseModel](self, call: LLMCall, schema: type[T]) -> LLMResult[T]:
        output_config: OutputConfigParam = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}
        }
        if call.effort is not None:
            output_config["effort"] = _effort(call.effort)
        # The system prompt holds the stable part (instructions + taxonomy), so caching
        # it makes every extraction after the first read it at a tenth of the price.
        # Below the model's minimum cacheable length this is silently a no-op.
        system: list[TextBlockParam] = [
            {"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[MessageParam] = [{"role": "user", "content": call.user}]

        started = time.monotonic()
        try:
            message = self._client.messages.create(
                model=call.model,
                max_tokens=call.max_tokens,
                system=system,
                messages=messages,
                output_config=output_config,
            )
        except anthropic.APIStatusError as e:
            return self._error(
                call,
                started,
                f"HTTP {e.status_code}: {e.message}",
                retryable=_is_retryable_status(e.status_code),
            )
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            return self._error(call, started, f"{type(e).__name__}: {e}", retryable=True)
        except anthropic.AnthropicError as e:  # e.g. a malformed response body
            return self._error(call, started, f"{type(e).__name__}: {e}", retryable=False)
        return self._result(call, schema, message, started)

    def _result[T: BaseModel](
        self, call: LLMCall, schema: type[T], message: Message, started: float
    ) -> LLMResult[T]:
        # Thinking blocks come first on Haiku 5.5; only text blocks hold the answer.
        texts = [block.text for block in message.content if block.type == "text"]
        raw_text = "".join(texts) if texts else None

        parsed: T | None = None
        outcome: Outcome
        error: str | None = None
        if message.stop_reason == "refusal":
            outcome = "refused"
            details = message.stop_details
            category = details.category if details else None
            explanation = details.explanation if details else None
            error = f"refusal (category={category}): {explanation or 'no explanation'}"
        elif message.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            # Partial JSON is useless as data but kept in raw_text for debugging.
            outcome = "truncated"
            error = f"stop_reason={message.stop_reason} at max_tokens={call.max_tokens}"
        elif raw_text is None:
            outcome = "invalid"
            error = f"no text in response (stop_reason={message.stop_reason})"
        else:
            try:
                parsed = schema.model_validate_json(raw_text)
                outcome = "ok"
            except ValidationError as e:
                outcome = "invalid"
                error = describe_validation_error(e)

        usage = message.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        # Optional in the SDK types: absent when no caching happened.
        cache_read = usage.cache_read_input_tokens or 0
        cache_write = usage.cache_creation_input_tokens or 0
        return LLMResult(
            parsed=parsed,
            raw_text=raw_text,
            outcome=outcome,
            error=error,
            retryable=False,
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


def _effort(value: str) -> Effort:
    # A typo in prompt frontmatter would otherwise surface as a 400 on every call.
    try:
        return _EFFORTS[value]
    except KeyError:
        raise ValueError(
            f"unknown Anthropic effort {value!r}; expected one of {sorted(_EFFORTS)}"
        ) from None


def _is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUSES or status >= 500


def _elapsed_ms(started: float) -> int:
    # Includes the SDK's own retry waits: this is how long the caller was blocked.
    return round((time.monotonic() - started) * 1000)
