"""OpenTelemetry tracing of LLM calls, exported to Langfuse.

Why OTel rather than the Langfuse SDK: the spans are vendor-neutral (any OTLP backend
can read them) and we stay free of an extra framework, in line with "provider SDKs
directly, no agent framework". Langfuse reads plain OTLP/HTTP and maps a documented set
of span attributes onto its own model (https://langfuse.com/integrations/native/opentelemetry):

    langfuse.observation.type            "generation" -> shown as an LLM call
    langfuse.observation.input / output  JSON strings
    langfuse.observation.model.name      plus gen_ai.request.model / gen_ai.response.model
    langfuse.observation.model.parameters
    langfuse.observation.usage_details   JSON {input, output, cache_read, cache_write}
    langfuse.observation.cost_details    JSON {"total": usd}
    langfuse.observation.prompt.name / .version
    langfuse.observation.level           ERROR for any outcome other than ok
    langfuse.observation.status_message
    langfuse.observation.metadata.*      anything else we want filterable

Tracing is optional: without Langfuse keys every span is a no-op and the pipeline runs
the same. `llm_runs` (Postgres) stays the system of record for cost; traces are for
looking at individual calls.

Privacy: a call marked `sensitive` (resumes) never carries input or output text, only
counts, model and outcome. That rule lives here, in one place, rather than in callers.
"""

import base64
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, Status, StatusCode, format_trace_id

from jobradar.llm.base import LLMCall, LLMResult
from jobradar.llm_runs import safe_error

logger = logging.getLogger(__name__)

DEFAULT_LANGFUSE_HOST = "https://us.cloud.langfuse.com"
_OTLP_TRACES_PATH = "/api/public/otel/v1/traces"
# Without this header Langfuse may delay OTel data by up to 10 minutes (its docs);
# version 4 turns on real-time ingestion.
_INGESTION_VERSION = "4"

# Module state rather than the global OTel provider: the global can be set only once per
# process, which would make init_tracing impossible to test or re-run.
_tracer: trace.Tracer | None = None
_initialized = False


def init_tracing(*, exporter: SpanExporter | None = None) -> bool:
    """Set up export to Langfuse. Returns True if spans will be exported.

    Idempotent: later calls return the first call's answer. Returns False (and every
    llm_span becomes a no-op) when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are missing.
    Never raises: tracing must not be able to break extraction.

    `exporter` is for tests (an InMemorySpanExporter): it skips the env check and
    exports synchronously, so a test can read spans right after the `with` block.
    """
    global _tracer, _initialized
    if _initialized:
        return _tracer is not None
    try:
        if exporter is not None:
            processor: SimpleSpanProcessor | BatchSpanProcessor = SimpleSpanProcessor(exporter)
        else:
            otlp = _langfuse_exporter()
            if otlp is None:
                logger.info("tracing off: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
                _initialized = True
                return False
            # Batching keeps export off the request path; the provider flushes at exit.
            processor = BatchSpanProcessor(otlp)
        provider = TracerProvider(resource=Resource.create({"service.name": "jobradar"}))
        provider.add_span_processor(processor)
        _tracer = provider.get_tracer("jobradar.llm")
    except Exception:
        logger.warning("tracing off: setup failed", exc_info=True)
        _tracer = None
    _initialized = True
    return _tracer is not None


def _langfuse_exporter() -> OTLPSpanExporter | None:
    target = langfuse_target()
    if target is None:
        return None
    endpoint, headers = target
    return OTLPSpanExporter(endpoint=endpoint, headers=headers)


def langfuse_target() -> tuple[str, dict[str, str]] | None:
    """(endpoint, headers) for Langfuse's OTLP/HTTP traces endpoint, from the env, or
    None when the keys are missing. Split out so it can be tested without a network."""
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        return None
    host = (os.environ.get("LANGFUSE_HOST") or DEFAULT_LANGFUSE_HOST).rstrip("/")
    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "x-langfuse-ingestion-version": _INGESTION_VERSION,
    }
    return f"{host}{_OTLP_TRACES_PATH}", headers


def _reset_for_tests() -> None:
    """Forget init_tracing's state so a test can initialize again."""
    global _tracer, _initialized
    _tracer = None
    _initialized = False


class LLMSpan:
    """Handle for the span around one LLM call. With tracing off it does nothing."""

    def __init__(self, span: Span | None, call: LLMCall) -> None:
        self._span = span
        self._call = call
        # 32 hex chars, stored in llm_runs.trace_id to jump from a row to the trace.
        self.trace_id: str | None = (
            format_trace_id(span.get_span_context().trace_id) if span is not None else None
        )

    def set_result(self, result: LLMResult[Any]) -> None:
        span = self._span
        if span is None:
            return
        attrs: dict[str, str | int] = {
            "gen_ai.response.model": result.model,
            "langfuse.observation.model.name": result.model,
            "gen_ai.usage.input_tokens": result.input_tokens,
            "gen_ai.usage.output_tokens": result.output_tokens,
            "langfuse.observation.usage_details": json.dumps(
                {
                    "input": result.input_tokens,
                    "output": result.output_tokens,
                    "cache_read": result.cache_read_tokens,
                    "cache_write": result.cache_write_tokens,
                }
            ),
            "langfuse.observation.metadata.outcome": result.outcome,
            "langfuse.observation.metadata.latency_ms": result.latency_ms,
        }
        # No cost when the model isn't priced: an absent cost is honest, 0 would not be.
        if result.cost_usd is not None:
            attrs["langfuse.observation.cost_details"] = json.dumps({"total": result.cost_usd})
        if not self._call.sensitive:
            output = _output_json(result)
            if output is not None:
                attrs["langfuse.observation.output"] = output
        span.set_attributes(attrs)
        if result.outcome != "ok":
            # safe_error: a validation error on a resume call can quote resume text.
            message = safe_error(self._call, result) or result.outcome
            span.set_attribute("langfuse.observation.level", "ERROR")
            span.set_attribute("langfuse.observation.status_message", message)
            span.set_status(Status(StatusCode.ERROR, message))


def _output_json(result: LLMResult[Any]) -> str | None:
    if result.parsed is not None:
        return str(result.parsed.model_dump_json())
    if result.raw_text is not None:
        return json.dumps({"raw_text": result.raw_text})
    return None


@contextmanager
def llm_span(call: LLMCall) -> Iterator[LLMSpan]:
    """A Langfuse "generation" span around one provider call.

    Usage: `with llm_span(call) as span: result = provider.complete(...);
    span.set_result(result)`. An exception inside the block is recorded on the span and
    re-raised.
    """
    if _tracer is None:
        yield LLMSpan(None, call)
        return
    attrs: dict[str, str | int] = {
        "langfuse.observation.type": "generation",
        "gen_ai.system": call.provider,
        "gen_ai.request.model": call.model,
        "gen_ai.request.max_tokens": call.max_tokens,
        "langfuse.observation.model.parameters": json.dumps(
            {"max_tokens": call.max_tokens, "effort": call.effort}
        ),
        # prompt.name/version link to Langfuse prompt management when a prompt with that
        # name exists there; our prompts live in git, so the metadata copies keep them
        # visible and filterable either way.
        "langfuse.observation.prompt.name": call.prompt_name,
        "langfuse.observation.prompt.version": call.prompt_version,
        "langfuse.observation.metadata.purpose": call.purpose,
        "langfuse.observation.metadata.prompt_name": call.prompt_name,
        "langfuse.observation.metadata.prompt_version": call.prompt_version,
        "langfuse.observation.metadata.sensitive": str(call.sensitive).lower(),
    }
    if not call.sensitive:
        attrs["langfuse.observation.input"] = json.dumps(
            [
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ]
        )
    with _tracer.start_as_current_span(f"{call.purpose} {call.model}", attributes=attrs) as span:
        yield LLMSpan(span, call)
