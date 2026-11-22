import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from pydantic import BaseModel

from jobradar.llm.base import LLMCall, LLMResult
from jobradar.llm_runs import cost_summary, format_summary, record_run, safe_error


class Out(BaseModel):
    answer: str


def make_call(**overrides: Any) -> LLMCall:
    fields: dict[str, Any] = {
        "purpose": "extract_posting",
        "prompt_name": "extract_posting",
        "prompt_version": 1,
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "system": "SYNTHETIC SYSTEM",
        "user": "SYNTHETIC USER",
        "max_tokens": 8000,
    }
    return LLMCall(**(fields | overrides))


def make_result(**overrides: Any) -> LLMResult[Any]:
    fields: dict[str, Any] = {
        "parsed": Out(answer="SYNTHETIC OUTPUT"),
        "raw_text": '{"answer": "SYNTHETIC OUTPUT"}',
        "outcome": "ok",
        "error": None,
        "retryable": False,
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 800,
        "cache_write_tokens": 50,
        "latency_ms": 1500,
        "cost_usd": 0.000214,
    }
    return LLMResult(**(fields | overrides))


def run_row(conn: psycopg.Connection[Any], run_id: int) -> dict[str, Any]:
    cur = conn.execute("SELECT * FROM llm_runs WHERE id = %s", (run_id,))
    row = cur.fetchone()
    assert row is not None and cur.description is not None
    return dict(zip([c.name for c in cur.description], row, strict=True))


def make_posting(conn: psycopg.Connection[Any]) -> int:
    board = conn.execute(
        "INSERT INTO boards (ats, board_token, company) VALUES ('greenhouse', %s, 'Test Co')"
        " RETURNING id",
        (f"test-llm-runs-{uuid.uuid4().hex[:12]}",),
    ).fetchone()
    assert board is not None
    row = conn.execute(
        "INSERT INTO postings (board_id, external_id, url, title, description_html,"
        " cleaned_text, content_hash, cleaner_version, ats_raw)"
        " VALUES (%s, 'ext-1', 'https://example.test/job', 'Synthetic Engineer', '<p>x</p>',"
        " 'x', 'hash', 1, '{}') RETURNING id",
        (board[0],),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def empty_runs(db: psycopg.Connection[Any]) -> None:
    # posting_facts.llm_run_id is ON DELETE SET NULL; the db fixture rolls this back.
    db.execute("DELETE FROM llm_runs")


# --- record_run ---


def test_record_ok_run(db: psycopg.Connection[Any]) -> None:
    posting = make_posting(db)

    run_id = record_run(db, make_call(), make_result(), posting_id=posting, trace_id="ab" * 16)

    row = run_row(db, run_id)
    assert row["purpose"] == "extract_posting"
    assert (row["provider"], row["model"]) == ("anthropic", "claude-haiku-5-5")
    assert (row["prompt_name"], row["prompt_version"]) == ("extract_posting", 1)
    assert row["posting_id"] == posting
    assert row["attempt"] == 1
    assert (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_tokens"],
        row["cache_write_tokens"],
    ) == (1000, 200, 800, 50)
    assert float(row["cost_usd"]) == pytest.approx(0.000214)
    assert row["latency_ms"] == 1500
    assert row["outcome"] == "ok"
    assert row["error"] is None
    assert row["output"] == {"answer": "SYNTHETIC OUTPUT"}
    assert row["trace_id"] == "ab" * 16


def test_record_uses_result_model(db: psycopg.Connection[Any]) -> None:
    # The provider may report a dated snapshot of the requested alias; record what ran.
    run_id = record_run(db, make_call(), make_result(model="claude-haiku-5-5-20261101"))
    assert run_row(db, run_id)["model"] == "claude-haiku-5-5-20261101"


def test_record_invalid_keeps_raw_text(db: psycopg.Connection[Any]) -> None:
    result = make_result(parsed=None, raw_text="{oops", outcome="invalid", error="bad JSON")

    row = run_row(db, record_run(db, make_call(), result, attempt=2))

    assert row["output"] == {"raw_text": "{oops"}
    assert (row["outcome"], row["error"], row["attempt"]) == ("invalid", "bad JSON", 2)


def test_record_api_error_without_output(db: psycopg.Connection[Any]) -> None:
    result = make_result(
        parsed=None,
        raw_text=None,
        outcome="api_error",
        error="529 overloaded",
        retryable=True,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0.0,
    )

    row = run_row(db, record_run(db, make_call(), result))

    assert row["output"] is None
    assert row["outcome"] == "api_error"


def test_record_unknown_cost_is_null(db: psycopg.Connection[Any]) -> None:
    row = run_row(db, record_run(db, make_call(), make_result(cost_usd=None)))
    assert row["cost_usd"] is None


def test_sensitive_run_stores_no_text(db: psycopg.Connection[Any]) -> None:
    call = make_call(purpose="extract_profile", prompt_name="extract_profile", sensitive=True)
    result = make_result(
        parsed=None,
        raw_text='{"skills": "SYNTHETIC RESUME DETAIL"}',
        outcome="invalid",
        error="validation error: input_value='SYNTHETIC RESUME DETAIL'",
    )

    row = run_row(db, record_run(db, call, result))

    assert row["output"] is None
    assert "SYNTHETIC RESUME DETAIL" not in (row["error"] or "")
    assert row["error"] == "invalid (details withheld: sensitive call)"
    assert row["input_tokens"] == 1000  # counts and cost are still recorded


def test_safe_error_keeps_api_errors_for_sensitive_calls() -> None:
    # Provider error messages describe the request failure, not its content.
    call = make_call(sensitive=True)
    result = make_result(parsed=None, outcome="api_error", error="429 rate limited")
    assert safe_error(call, result) == "429 rate limited"


def test_record_links_profile_and_match_run(db: psycopg.Connection[Any]) -> None:
    profile = db.execute(
        "INSERT INTO profiles (name, facts, filters) VALUES (%s, '{}', '{}') RETURNING id",
        (f"synthetic-{uuid.uuid4().hex[:8]}",),
    ).fetchone()
    assert profile is not None
    match_run = db.execute(
        "INSERT INTO match_runs (profile_id, status) VALUES (%s, 'ok') RETURNING id",
        (profile[0],),
    ).fetchone()
    assert match_run is not None
    call = make_call(purpose="rerank", prompt_name="rerank", provider="openai", model="gpt-6.2-sol")

    run_id = record_run(
        db,
        call,
        make_result(provider="openai", model="gpt-6.2-sol"),
        profile_id=profile[0],
        match_run_id=match_run[0],
    )

    row = run_row(db, run_id)
    assert (row["profile_id"], row["match_run_id"]) == (profile[0], match_run[0])


# --- cost_summary ---


def test_cost_summary_groups_and_aggregates(db: psycopg.Connection[Any], empty_runs: None) -> None:
    for latency, cost in ((100, 0.001), (200, 0.002), (300, 0.003)):
        record_run(db, make_call(), make_result(latency_ms=latency, cost_usd=cost))
    record_run(
        db,
        make_call(),
        make_result(parsed=None, outcome="invalid", latency_ms=400, cost_usd=None),
        attempt=2,
    )
    record_run(db, make_call(prompt_version=2), make_result(latency_ms=50, cost_usd=0.01))

    rows = cost_summary(db, since=datetime.now(UTC) - timedelta(days=7))

    assert [(r["purpose"], r["model"], r["prompt_version"]) for r in rows] == [
        ("extract_posting", "claude-haiku-5-5", 1),
        ("extract_posting", "claude-haiku-5-5", 2),
    ]
    v1 = rows[0]
    assert v1["runs"] == 4
    assert v1["total_cost_usd"] == pytest.approx(0.006)
    assert v1["avg_cost_usd"] == pytest.approx(0.002)  # over priced runs only
    assert v1["unpriced_runs"] == 1
    assert v1["avg_latency_ms"] == pytest.approx(250.0)
    # percentile_cont interpolates: 95th of [100, 200, 300, 400] = 300 + 0.85 * 100.
    assert v1["p95_latency_ms"] == pytest.approx(385.0)
    assert (v1["ok"], v1["invalid"], v1["refused"], v1["truncated"], v1["api_error"]) == (
        3,
        1,
        0,
        0,
        0,
    )
    assert v1["input_tokens"] == 4000
    assert rows[1]["runs"] == 1


def test_cost_summary_respects_since(db: psycopg.Connection[Any], empty_runs: None) -> None:
    old = record_run(db, make_call(), make_result())
    db.execute("UPDATE llm_runs SET created_at = now() - interval '30 days' WHERE id = %s", (old,))
    record_run(db, make_call(), make_result())

    rows = cost_summary(db, since=datetime.now(UTC) - timedelta(days=7))

    assert [r["runs"] for r in rows] == [1]


def test_cost_summary_empty(db: psycopg.Connection[Any], empty_runs: None) -> None:
    assert cost_summary(db, since=datetime.now(UTC) - timedelta(days=7)) == []


def test_format_summary_renders_table() -> None:
    rows: list[dict[str, Any]] = [
        {
            "purpose": "extract_posting",
            "model": "claude-haiku-5-5",
            "prompt_version": 1,
            "runs": 4,
            "total_cost_usd": 0.006,
            "avg_cost_usd": 0.002,
            "unpriced_runs": 1,
            "avg_latency_ms": 250.0,
            "p95_latency_ms": 385.0,
            "ok": 3,
            "invalid": 1,
            "refused": 0,
            "truncated": 0,
            "api_error": 0,
            "input_tokens": 4000,
            "output_tokens": 800,
            "cache_read_tokens": 3200,
            "cache_write_tokens": 200,
        }
    ]

    text = format_summary(rows)

    lines = text.splitlines()
    assert "purpose" in lines[0] and "p95_ms" in lines[0]
    assert "extract_posting" in text and "0.006000" in text
    assert "TOTAL" in lines[-1]


def test_format_summary_empty() -> None:
    assert "no LLM runs" in format_summary([])
