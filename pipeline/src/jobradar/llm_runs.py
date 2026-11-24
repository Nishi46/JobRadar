"""The LLM call log: one `llm_runs` row per provider call, whatever its outcome.

Like `raw_fetches`, this is evidence, not state (D-009). Each row is committed in its
own transaction right after the call returns, before the caller validates the output
or opens its state transaction. A refused, truncated or invalid call still cost money,
and those failures are exactly the ones that roll the state transaction back, so a log
written inside it would lose the rows we most need for cost accounting and debugging.
(As with raw_fetches, the commit is real only on an autocommit connection, which the
workers use; inside a test's outer transaction it is a savepoint.)

Privacy: for a call marked `sensitive` (resumes) no model text is stored: `output` is
NULL and validation error messages are withheld, because Pydantic errors quote the
offending input. Token counts, cost, latency and outcome are kept.

`python -m jobradar.llm_runs [--days N]` prints `cost_summary` as a table (`make cost`).
"""

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from jobradar.db import connect
from jobradar.llm.base import LLMCall, LLMResult

MAX_ERROR_CHARS = 2000

_INSERT = """
    INSERT INTO llm_runs (
        purpose, provider, model, prompt_name, prompt_version,
        posting_id, profile_id, match_run_id, attempt,
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
        cost_usd, latency_ms, outcome, error, output, trace_id
    ) VALUES (
        %(purpose)s, %(provider)s, %(model)s, %(prompt_name)s, %(prompt_version)s,
        %(posting_id)s, %(profile_id)s, %(match_run_id)s, %(attempt)s,
        %(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s, %(cache_write_tokens)s,
        %(cost_usd)s, %(latency_ms)s, %(outcome)s, %(error)s, %(output)s, %(trace_id)s
    )
    RETURNING id
"""

# One row per (purpose, model, prompt_version): the unit a prompt or model change moves.
# avg(cost_usd) skips NULLs, so it is the average over *priced* runs; unpriced_runs says
# how many were left out (a model missing from the price table is unknown, not free).
# percentile_cont interpolates between neighbors, so p95 of a handful of runs is
# meaningful-ish rather than jumping to the max.
_SUMMARY = """
    SELECT purpose, model, prompt_version,
           count(*)::int                                        AS runs,
           coalesce(sum(cost_usd), 0)::float8                   AS total_cost_usd,
           avg(cost_usd)::float8                                AS avg_cost_usd,
           count(*) FILTER (WHERE cost_usd IS NULL)::int        AS unpriced_runs,
           avg(latency_ms)::float8                              AS avg_latency_ms,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::float8
                                                                AS p95_latency_ms,
           count(*) FILTER (WHERE outcome = 'ok')::int          AS ok,
           count(*) FILTER (WHERE outcome = 'invalid')::int     AS invalid,
           count(*) FILTER (WHERE outcome = 'refused')::int     AS refused,
           count(*) FILTER (WHERE outcome = 'truncated')::int   AS truncated,
           count(*) FILTER (WHERE outcome = 'api_error')::int   AS api_error,
           sum(input_tokens)::bigint                            AS input_tokens,
           sum(output_tokens)::bigint                           AS output_tokens,
           sum(cache_read_tokens)::bigint                       AS cache_read_tokens,
           sum(cache_write_tokens)::bigint                      AS cache_write_tokens
    FROM llm_runs
    WHERE created_at >= %s
    GROUP BY purpose, model, prompt_version
    ORDER BY purpose, model, prompt_version
"""


def safe_error(call: LLMCall, result: LLMResult[Any]) -> str | None:
    """The error text that may be stored or traced for this call.

    For sensitive calls only API errors are kept: they describe the request (429,
    timeout), while a validation error can quote resume-derived model output.
    """
    if result.error is None:
        return None
    if call.sensitive and result.outcome != "api_error":
        return f"{result.outcome} (details withheld: sensitive call)"
    return result.error[:MAX_ERROR_CHARS]


def _output(call: LLMCall, result: LLMResult[Any]) -> Jsonb | None:
    if call.sensitive:
        return None
    if result.parsed is not None:
        return Jsonb(result.parsed.model_dump(mode="json"))
    if result.raw_text is not None:
        # Wrapped, because invalid output is often not JSON at all.
        return Jsonb({"raw_text": result.raw_text})
    return None


def record_run(
    conn: psycopg.Connection[Any],
    call: LLMCall,
    result: LLMResult[Any],
    *,
    attempt: int = 1,
    posting_id: int | None = None,
    profile_id: int | None = None,
    match_run_id: int | None = None,
    trace_id: str | None = None,
) -> int:
    """Insert one llm_runs row and commit it. Returns the row id.

    Call it immediately after `provider.complete`, outside any state transaction.
    `model` comes from the result (what actually ran), the rest of the call identity
    from the call.
    """
    params = {
        "purpose": call.purpose,
        "provider": call.provider,
        "model": result.model,
        "prompt_name": call.prompt_name,
        "prompt_version": call.prompt_version,
        "posting_id": posting_id,
        "profile_id": profile_id,
        "match_run_id": match_run_id,
        "attempt": attempt,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "outcome": result.outcome,
        "error": safe_error(call, result),
        "output": _output(call, result),
        "trace_id": trace_id,
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_INSERT, params)
        row = cur.fetchone()
        assert row is not None
        run_id: int = row[0]
        return run_id


def cost_summary(conn: psycopg.Connection[Any], since: datetime) -> list[dict[str, Any]]:
    """Per (purpose, model, prompt_version) since `since`: run count, total/avg cost,
    avg/p95 latency, outcome counts and token sums. Costs are USD floats."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SUMMARY, (since,))
        return list(cur.fetchall())


# Columns of the printed table: (header, row key, format).
_COLUMNS: list[tuple[str, str, str]] = [
    ("purpose", "purpose", "{}"),
    ("model", "model", "{}"),
    ("v", "prompt_version", "{}"),
    ("runs", "runs", "{}"),
    ("ok", "ok", "{}"),
    ("invalid", "invalid", "{}"),
    ("refused", "refused", "{}"),
    ("trunc", "truncated", "{}"),
    ("api_err", "api_error", "{}"),
    ("total_usd", "total_cost_usd", "{:.6f}"),
    ("avg_usd", "avg_cost_usd", "{:.6f}"),
    ("unpriced", "unpriced_runs", "{}"),
    ("avg_ms", "avg_latency_ms", "{:.0f}"),
    ("p95_ms", "p95_latency_ms", "{:.0f}"),
]


def format_summary(rows: list[dict[str, Any]]) -> str:
    """cost_summary rows as a plain-text table with a TOTAL line."""
    if not rows:
        return "no LLM runs in this window"
    headers = [h for h, _, _ in _COLUMNS]
    body = [
        ["-" if r[key] is None else fmt.format(r[key]) for _, key, fmt in _COLUMNS] for r in rows
    ]
    total = sum(float(r["total_cost_usd"]) for r in rows)
    total_runs = sum(int(r["runs"]) for r in rows)
    widths = [max(len(cell) for cell in col) for col in zip(headers, *body, strict=True)]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(w) for cell, w in zip(cells, widths, strict=True)).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(cells) for cells in body]
    out.append(f"TOTAL  {total_runs} runs  ${total:.6f}")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM cost and latency summary (llm_runs).")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    args = parser.parse_args()
    since = datetime.now(UTC) - timedelta(days=args.days)
    with connect() as conn:
        rows = cost_summary(conn, since)
    print(f"LLM runs since {since:%Y-%m-%d %H:%M} UTC ({args.days} days)")
    print(format_summary(rows))


if __name__ == "__main__":
    main()
