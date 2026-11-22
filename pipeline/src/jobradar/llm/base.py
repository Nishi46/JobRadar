"""The provider-neutral contract every LLM call goes through.

Callers (extraction, profiles, re-rank) build an `LLMCall` from a prompt file and get
back an `LLMResult`. Every way a call can go wrong (API error, refusal, truncation,
output that fails the schema) is a value, not an exception, because each one is a
normal, expected outcome that must be recorded in `llm_runs` with its tokens and cost:
a truncated response still cost money, and the repair retry needs the broken text.

Token counts keep each provider's own meaning, so `llm_runs` matches the provider's
dashboard and bill:
- Anthropic: `input_tokens` excludes cached tokens; cache reads and writes are extra.
- OpenAI: `input_tokens` includes cached tokens (`cache_read_tokens` is a subset).
`pricing.cost_usd` knows which convention each model uses.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

Outcome = Literal["ok", "invalid", "refused", "truncated", "api_error"]


@dataclass(frozen=True)
class LLMCall:
    purpose: Literal["extract_posting", "extract_profile", "rerank"]
    prompt_name: str
    prompt_version: int
    provider: Literal["anthropic", "openai"]
    model: str
    system: str
    user: str
    max_tokens: int
    # Anthropic output_config.effort / OpenAI reasoning.effort; None = provider default.
    effort: str | None = None
    # True for resumes: input/output text must never be traced or stored.
    sensitive: bool = False


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    parsed: T | None  # set only when outcome == "ok"
    # The model's text output when there was any; kept for the repair retry and llm_runs.
    raw_text: str | None
    outcome: Outcome
    error: str | None
    # Meaningful for api_error: True when the failure is transient (429, 5xx, connection)
    # and survived the SDK's own retries, so the job queue should try again later.
    retryable: bool
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    latency_ms: int
    cost_usd: float | None  # None when the model has no price entry


class Provider(Protocol):
    name: str

    def complete[T: BaseModel](self, call: LLMCall, schema: type[T]) -> LLMResult[T]:
        """Run one call and validate the output against `schema`. Never raises for API
        errors, refusals, truncation or schema-invalid output."""
        ...


def describe_validation_error(error: ValidationError) -> str:
    """One line per problem, e.g. "score: Input should be a valid integer".

    Built from `errors(include_input=False)` because pydantic's default message quotes
    the offending value, and for resumes that value is personal data that must never
    reach a log line or the `llm_runs.error` column.
    """
    parts = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
