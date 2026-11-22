import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel

from jobradar import tracing
from jobradar.llm.base import LLMCall, LLMResult
from jobradar.tracing import init_tracing, langfuse_target, llm_span

SYSTEM = "SYNTHETIC SYSTEM PROMPT"
USER = "SYNTHETIC USER TEXT"


class Out(BaseModel):
    answer: str


def make_call(*, sensitive: bool = False) -> LLMCall:
    return LLMCall(
        purpose="extract_profile" if sensitive else "extract_posting",
        prompt_name="extract_posting",
        prompt_version=1,
        provider="anthropic",
        model="claude-haiku-5-5",
        system=SYSTEM,
        user=USER,
        max_tokens=8000,
        effort="low",
        sensitive=sensitive,
    )


def make_result(**overrides: Any) -> LLMResult[Any]:
    fields: dict[str, Any] = {
        "parsed": Out(answer="SYNTHETIC OUTPUT"),
        "raw_text": '{"answer": "SYNTHETIC OUTPUT"}',
        "outcome": "ok",
        "error": None,
        "retryable": False,
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "input_tokens": 1200,
        "output_tokens": 300,
        "cache_read_tokens": 900,
        "cache_write_tokens": 0,
        "latency_ms": 2100,
        "cost_usd": 0.000279,
    }
    return LLMResult(**(fields | overrides))


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    tracing._reset_for_tests()
    mem = InMemorySpanExporter()
    assert init_tracing(exporter=mem) is True
    yield mem
    tracing._reset_for_tests()


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def test_generation_span_attributes(exporter: InMemorySpanExporter) -> None:
    with llm_span(make_call()) as span:
        span.set_result(make_result())

    s = only_span(exporter)
    a = attrs(s)
    assert s.name == "extract_posting claude-haiku-5-5"
    assert a["langfuse.observation.type"] == "generation"
    assert a["gen_ai.system"] == "anthropic"
    assert a["gen_ai.request.model"] == "claude-haiku-5-5"
    assert a["langfuse.observation.prompt.name"] == "extract_posting"
    assert a["langfuse.observation.prompt.version"] == 1
    assert json.loads(a["langfuse.observation.usage_details"]) == {
        "input": 1200,
        "output": 300,
        "cache_read": 900,
        "cache_write": 0,
    }
    assert json.loads(a["langfuse.observation.cost_details"]) == {"total": 0.000279}
    messages = json.loads(a["langfuse.observation.input"])
    assert [m["content"] for m in messages] == [SYSTEM, USER]
    assert json.loads(a["langfuse.observation.output"]) == {"answer": "SYNTHETIC OUTPUT"}
    assert s.status.status_code != StatusCode.ERROR


def test_trace_id_is_32_hex(exporter: InMemorySpanExporter) -> None:
    with llm_span(make_call()) as span:
        trace_id = span.trace_id

    assert trace_id is not None
    assert len(trace_id) == 32
    int(trace_id, 16)
    assert trace_id == format(only_span(exporter).context.trace_id, "032x")


def test_sensitive_call_has_no_input_or_output(exporter: InMemorySpanExporter) -> None:
    with llm_span(make_call(sensitive=True)) as span:
        span.set_result(make_result())

    a = attrs(only_span(exporter))
    assert "langfuse.observation.input" not in a
    assert "langfuse.observation.output" not in a
    # Belt and braces: the text appears in no attribute at all.
    blob = json.dumps(a)
    for text in (SYSTEM, USER, "SYNTHETIC OUTPUT"):
        assert text not in blob
    assert a["langfuse.observation.usage_details"]  # counts are still traced


def test_sensitive_error_message_is_withheld(exporter: InMemorySpanExporter) -> None:
    result = make_result(
        parsed=None, raw_text=None, outcome="invalid", error="input_value='SYNTHETIC SECRET'"
    )
    with llm_span(make_call(sensitive=True)) as span:
        span.set_result(result)

    s = only_span(exporter)
    assert "SYNTHETIC SECRET" not in json.dumps(attrs(s))
    assert "SYNTHETIC SECRET" not in (s.status.description or "")


def test_invalid_output_traces_raw_text_and_error(exporter: InMemorySpanExporter) -> None:
    with llm_span(make_call()) as span:
        span.set_result(
            make_result(parsed=None, raw_text="not json", outcome="invalid", error="bad JSON")
        )

    s = only_span(exporter)
    a = attrs(s)
    assert json.loads(a["langfuse.observation.output"]) == {"raw_text": "not json"}
    assert a["langfuse.observation.level"] == "ERROR"
    assert a["langfuse.observation.status_message"] == "bad JSON"
    assert s.status.status_code == StatusCode.ERROR


def test_unknown_cost_is_omitted(exporter: InMemorySpanExporter) -> None:
    with llm_span(make_call()) as span:
        span.set_result(make_result(cost_usd=None))

    assert "langfuse.observation.cost_details" not in attrs(only_span(exporter))


def test_exception_is_recorded_and_reraised(exporter: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError), llm_span(make_call()):
        raise RuntimeError("boom")

    assert only_span(exporter).status.status_code == StatusCode.ERROR


def test_off_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    tracing._reset_for_tests()
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    try:
        assert init_tracing() is False
        assert init_tracing() is False  # idempotent
        with llm_span(make_call()) as span:
            span.set_result(make_result())  # no-op, no error
        assert span.trace_id is None
    finally:
        tracing._reset_for_tests()


def test_langfuse_exporter_endpoint_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.test/")

    target = langfuse_target()

    assert target is not None
    endpoint, headers = target
    assert endpoint == "https://langfuse.example.test/api/public/otel/v1/traces"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert headers == {
        "Authorization": f"Basic {expected}",
        "x-langfuse-ingestion-version": "4",
    }


def test_langfuse_host_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)

    target = langfuse_target()

    assert target is not None
    assert target[0] == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"


def test_no_target_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    assert langfuse_target() is None
