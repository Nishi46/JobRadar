"""Build a provider by name (the `provider:` line of a prompt's frontmatter).

Clients are built on first request, not at import, so modules that never call a model
(tests, `make cost`) need no API keys. Each provider is built once per process and
reused, keeping the SDK's HTTP connection pool warm across a worker's many calls.

The key is read from the environment and checked here, so a missing key fails loudly
at startup with the variable's name instead of as an `api_error` on every posting.
"""

import os
from functools import cache

import anthropic
import openai

from jobradar.llm.anthropic_provider import AnthropicProvider
from jobradar.llm.base import Provider
from jobradar.llm.openai_provider import OpenAIProvider

# Transient errors get this many SDK retries (exponential backoff, Retry-After honored)
# before the call comes back as a retryable api_error for the job queue.
SDK_MAX_RETRIES = 2


class MissingAPIKeyError(RuntimeError):
    pass


def _key(env_var: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if not value:
        raise MissingAPIKeyError(f"{env_var} is not set; add it to .env (see .env.example)")
    return value


@cache
def get_provider(name: str) -> Provider:
    if name == "anthropic":
        return AnthropicProvider(
            client=anthropic.Anthropic(
                api_key=_key("ANTHROPIC_API_KEY"),
                max_retries=SDK_MAX_RETRIES,
                # The SDK emits its own OpenTelemetry spans by default. tracing.llm_span
                # already records each call (and leaves out resume text), so SDK spans
                # would duplicate every generation in Langfuse and could carry content
                # if ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE were ever set.
                open_telemetry=False,
            )
        )
    if name == "openai":
        return OpenAIProvider(
            client=openai.OpenAI(api_key=_key("OPENAI_API_KEY"), max_retries=SDK_MAX_RETRIES)
        )
    raise ValueError(f"unknown provider {name!r}; expected 'anthropic' or 'openai'")
