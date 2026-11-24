"""Turn one posting into a validated `posting_facts` row: call, check, repair once, store.

The flow for one posting (M2_SPEC section F):

1. Read the posting, its board, and its `content_hash` *now*, before any LLM call.
2. Call the model (attempt 1), inside a tracing span, and log it to `llm_runs` at once.
3. Check the answer in code: schema (the provider already did it), then validate_facts
   (evidence ids, taxonomy, salary numbers in the cited text...). Paragraph [0] is
   the rendered header (company, title, locations), derived, never stored.
4. If something is wrong and a retry could fix it, make ONE repair call (attempt 2):
   the same prompt plus the previous answer and the list of problems.
5. Cross-check pay against the ATS's structured compensation (DESIGN.md section 5).
   A disagreement on attempt 1 uses the same single retry. If it still disagrees,
   the ATS value is displayed and the disagreement is flagged.
6. Write the result in ONE state transaction, ending with the fenced `queue.complete`
   (D-008, same shape as ingest.apply_poll).

Why at most one retry, whatever the reason: it bounds the worst case at two calls per
posting, so the cost per posting is predictable (about 2x the median at worst). A
posting that is still wrong after a targeted repair is usually wrong for a reason a
third call won't fix (the posting is odd, or the prompt is), and the `failed` row with
its issues is exactly what M3 error analysis wants to read.

Which outcomes become what:

- ok + valid               -> status 'ok'
- invalid / validation     -> repair retry; still wrong -> status 'failed' + issues
- refused / truncated      -> status 'failed' at once. The same input would refuse or
                              overflow max_tokens again, so a retry is wasted money.
- api_error                -> raise ExtractionUnavailable, write NO facts row. An outage
                              says nothing about the posting; the queue retries the job
                              later (if retryable) and the posting stays stale until then.

`llm_runs` rows are written outside the state transaction (D-009): a rolled-back
extraction (lost lease, crash) still leaves the record of what we paid for and why.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import psycopg
from psycopg.types.json import Jsonb

from jobradar import queue
from jobradar.facts import PostingFacts, SalaryRange
from jobradar.llm.base import LLMCall, LLMResult, Provider
from jobradar.llm_runs import annotate_run, record_run
from jobradar.prompts import Prompt, load_prompt
from jobradar.render import (
    ats_hints_from_raw,
    header_paragraph,
    render_posting,
    render_taxonomy,
)
from jobradar.tracing import llm_span
from jobradar.validate import Issue, PayDecision, ats_salary_ranges, decide_pay, validate_facts

log = logging.getLogger(__name__)

PROMPT_NAME = "extract_posting"

# Everything the prompt and the checks need, read once at the start. content_hash is
# read here, not at write time, so an ingest that edits the posting mid-extraction
# leaves these facts marked with the OLD hash, i.e. stale, and they get redone.
_LOAD_POSTING = """
    SELECT p.title, p.locations_raw, p.departments, p.paragraphs, p.content_hash,
           p.ats_compensation, p.ats_raw, b.company, b.ats
    FROM postings p
    JOIN boards b ON b.id = p.board_id
    WHERE p.id = %s
"""

# One row per posting, overwritten on every extraction: posting_facts is current
# state, and the history of every attempt lives in llm_runs.
_UPSERT_FACTS = """
    INSERT INTO posting_facts (
        posting_id, status, facts, display_salary, pay_source, pay_disagreement, issues,
        content_hash, prompt_name, prompt_version, model, llm_run_id, extracted_at
    ) VALUES (
        %(posting_id)s, %(status)s, %(facts)s, %(display_salary)s, %(pay_source)s,
        %(pay_disagreement)s, %(issues)s, %(content_hash)s, %(prompt_name)s,
        %(prompt_version)s, %(model)s, %(llm_run_id)s, now()
    )
    ON CONFLICT (posting_id) DO UPDATE SET
        status           = EXCLUDED.status,
        facts            = EXCLUDED.facts,
        display_salary   = EXCLUDED.display_salary,
        pay_source       = EXCLUDED.pay_source,
        pay_disagreement = EXCLUDED.pay_disagreement,
        issues           = EXCLUDED.issues,
        content_hash     = EXCLUDED.content_hash,
        prompt_name      = EXCLUDED.prompt_name,
        prompt_version   = EXCLUDED.prompt_version,
        model            = EXCLUDED.model,
        llm_run_id       = EXCLUDED.llm_run_id,
        extracted_at     = EXCLUDED.extracted_at
"""

# Stale = needs (re-)extraction: never extracted, edited since (hash differs), or
# extracted by another prompt version. A row that *failed* on the current hash and
# version is not stale: failing again would cost the same and fail the same way.
# Closed postings are skipped; nobody is shown them. Oldest first, so a capped sample
# run (--limit) is deterministic. LIMIT NULL means no limit in Postgres.
_STALE = """
    SELECT p.id
    FROM postings p
    LEFT JOIN posting_facts f ON f.posting_id = p.id
    WHERE p.status = 'open'
      AND (f.posting_id IS NULL
           OR f.content_hash <> p.content_hash
           OR f.prompt_version IS DISTINCT FROM %(prompt_version)s)
    ORDER BY p.first_seen, p.id
    LIMIT %(limit)s
"""

# Appended to the original user message for the repair call. The model sees its own
# previous answer and our checks' findings, and is told to change only what's wrong,
# so a repair doesn't silently rewrite fields that were already right.
_REPAIR_TEMPLATE = """

## Your previous answer

{previous}

## Problems our checks found in it

{problems}

Fix these problems and keep everything else exactly as it was. Return the complete
corrected JSON."""

# The disagreement retry deliberately does NOT reveal the ATS numbers. If it did, the
# model would copy them, and ATS compensation would stop being an independent label for
# salary extraction in M3 (DESIGN.md section 5). It only asks for a careful re-read.
_PAY_RECHECK = Issue(
    field="salary.ranges",
    message=(
        "These salary ranges do not match the job board's structured pay data. Re-read "
        "the paragraph you cited and copy each number exactly as the text states it, "
        "with the right currency and pay type. If the text states no pay, return an "
        "empty ranges list with evidence null."
    ),
)


@dataclass(frozen=True)
class ExtractionOutcome:
    posting_id: int
    status: Literal["ok", "failed"]
    attempts: int  # LLM calls made: 1, or 2 when the repair retry ran
    issues: list[Issue]  # of the last attempt; [] when ok
    cost_usd: float  # all attempts; unpriced calls count as 0 (llm_runs keeps the NULL)
    latency_ms: int = 0  # all attempts, end to end per call
    pay_disagreement: bool = False


class ExtractionUnavailable(Exception):
    """The LLM API failed (outage, rate limit, auth...). Nothing about the posting was
    learned, so no facts are written; the worker turns this into a job failure."""

    def __init__(self, posting_id: int, error: str, *, retryable: bool, cost_usd: float) -> None:
        super().__init__(f"posting {posting_id}: LLM API error: {error}")
        self.posting_id = posting_id
        self.retryable = retryable
        self.cost_usd = cost_usd  # attempts before the error were still paid for


@dataclass(frozen=True)
class _Posting:
    company: str
    ats: str
    title: str
    locations: list[str]
    departments: list[str]
    paragraphs: list[dict[str, Any]]
    content_hash: str
    ats_compensation: dict[str, Any] | None
    ats_raw: dict[str, Any]


@dataclass
class _Attempts:
    """Running totals across the (at most two) calls for one posting."""

    count: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    run_ids: list[int] = field(default_factory=list)


def stale_posting_ids(
    conn: psycopg.Connection[Any], *, prompt_version: int, limit: int | None = None
) -> list[int]:
    """Open postings that need extraction with this prompt version, oldest first."""
    rows = conn.execute(_STALE, {"prompt_version": prompt_version, "limit": limit}).fetchall()
    return [r[0] for r in rows]


def extract_posting(
    conn: psycopg.Connection[Any],
    provider: Provider,
    posting_id: int,
    job: queue.Job | None = None,
    *,
    prompt: Prompt | None = None,
) -> ExtractionOutcome:
    """Extract, validate and store facts for one posting (see the module docstring).

    With `job`, the facts write and `queue.complete(job)` commit together, and
    queue.LeaseLost (raised after rolling the write back) means another worker owns the
    job now. Raises ExtractionUnavailable on an LLM API error (no facts written) and
    ValueError for an unknown posting. `prompt` defaults to the newest extract_posting
    version on disk; the worker passes it to avoid re-reading the file per posting.
    """
    prompt = prompt or load_prompt(PROMPT_NAME)
    posting = _load(conn, posting_id)
    ats_ranges = ats_salary_ranges(posting.ats, posting.ats_compensation)

    header: dict[str, Any] = {
        "company": posting.company,
        "title": posting.title,
        "locations": posting.locations,
        "departments": posting.departments,
        "ats_hints": ats_hints_from_raw(posting.ats, posting.ats_raw),
    }
    # Location, remote policy and seniority often appear only in the header (title,
    # ATS location fields), so the rendered posting shows it as paragraph [0] and the
    # model may cite it. It is derived on every render, never stored in
    # postings.paragraphs (that would change what the content hash covers, D-011).
    # The checks must see exactly what the model saw, so [0] is citable there too.
    cited = [header_paragraph(**header), *posting.paragraphs]
    system, user = prompt.render(
        posting=render_posting(**header, paragraphs=posting.paragraphs),
        taxonomy=render_taxonomy(),
    )
    first_call = LLMCall(
        purpose="extract_posting",
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        provider=_provider_literal(prompt.provider),
        model=prompt.model,
        system=system,
        user=user,
        max_tokens=prompt.max_tokens,
        effort=prompt.effort,
    )
    totals = _Attempts()

    result = _call(conn, provider, first_call, posting_id, totals)
    issues, repairable = _check(result, cited)
    pay = _pay(result, issues, ats_ranges)
    _record_verdict(conn, totals, result, issues, pay)

    # The single retry: for fixable problems first, otherwise for a pay disagreement.
    retry_for: list[Issue] = []
    if issues and repairable:
        retry_for = issues
    elif pay is not None and pay.disagreement:
        retry_for = [_PAY_RECHECK]
    if retry_for:
        repair_call = _repair_call(first_call, result, retry_for)
        result = _call(conn, provider, repair_call, posting_id, totals)
        issues, _ = _check(result, cited)
        pay = _pay(result, issues, ats_ranges)
        _record_verdict(conn, totals, result, issues, pay)

    ok = result.parsed is not None and not issues
    if ok and pay is not None and pay.disagreement:
        # Still disagrees after the retry (or the retry went to a validation fix):
        # the ATS value is shown, and the flag makes the disagreement queryable.
        log.warning("posting %s: pay disagrees with the ATS: %s", posting_id, pay.detail)

    _store(conn, posting_id, posting.content_hash, first_call, result, issues, pay, totals, job)
    return ExtractionOutcome(
        posting_id=posting_id,
        status="ok" if ok else "failed",
        attempts=totals.count,
        issues=[] if ok else issues,
        cost_usd=totals.cost_usd,
        latency_ms=totals.latency_ms,
        pay_disagreement=bool(ok and pay is not None and pay.disagreement),
    )


def _load(conn: psycopg.Connection[Any], posting_id: int) -> _Posting:
    row = conn.execute(_LOAD_POSTING, (posting_id,)).fetchone()
    if row is None:
        raise ValueError(f"posting {posting_id} does not exist")
    title, locations, departments, paragraphs, content_hash, comp, ats_raw, company, ats = row
    return _Posting(
        company=company,
        ats=ats,
        title=title,
        locations=list(locations),
        departments=list(departments),
        paragraphs=paragraphs,
        content_hash=content_hash,
        ats_compensation=comp,
        ats_raw=ats_raw,
    )


def _call(
    conn: psycopg.Connection[Any],
    provider: Provider,
    call: LLMCall,
    posting_id: int,
    totals: _Attempts,
) -> LLMResult[PostingFacts]:
    """One traced, logged LLM call. Raises ExtractionUnavailable on api_error, after the
    run is logged, so the failed call is still on record (D-009)."""
    totals.count += 1
    with llm_span(call) as span:
        result = provider.complete(call, PostingFacts)
        span.set_result(result)
    run_id = record_run(
        conn, call, result, attempt=totals.count, posting_id=posting_id, trace_id=span.trace_id
    )
    totals.run_ids.append(run_id)
    totals.cost_usd += result.cost_usd or 0.0
    totals.latency_ms += result.latency_ms
    if result.outcome == "api_error":
        raise ExtractionUnavailable(
            posting_id,
            result.error or "unknown API error",
            retryable=result.retryable,
            cost_usd=totals.cost_usd,
        )
    return result


def _check(
    result: LLMResult[PostingFacts], paragraphs: list[dict[str, Any]]
) -> tuple[list[Issue], bool]:
    """(issues, repairable). [] means the answer is good. Never sees api_error (_call
    raised already)."""
    if result.outcome == "ok" and result.parsed is not None:
        return validate_facts(result.parsed, paragraphs), True
    if result.outcome == "invalid":
        detail = result.error or "the output did not match the schema"
        return [
            Issue(field="schema", message=f"The answer did not match the schema: {detail}")
        ], True
    if result.outcome == "refused":
        return [Issue(field="refused", message=result.error or "the model refused")], False
    if result.outcome == "truncated":
        message = result.error or "the output hit max_tokens before it was complete"
        return [Issue(field="truncated", message=message)], False
    # "ok" without a parsed value would be a provider bug; treat it as unusable output.
    return [Issue(field="schema", message=f"no parsed output (outcome {result.outcome})")], True


def _record_verdict(
    conn: psycopg.Connection[Any],
    totals: _Attempts,
    result: LLMResult[PostingFacts],
    issues: list[Issue],
    pay: PayDecision | None,
) -> None:
    """Write our checks' verdict onto the llm_runs row of the attempt just made.

    The provider said "ok", but validation may have rejected the answer: that run becomes
    'invalid' with the issues as its error, so `make cost` counts it and M3 error analysis
    can see why a retry happened. A pay disagreement is not invalid (the answer is kept
    as the model's label), so the outcome stays 'ok' and only the reason is noted.
    Non-ok outcomes already carry the provider's own error and are left alone.
    """
    if result.outcome != "ok":
        return
    run_id = totals.run_ids[-1]
    if issues:
        reasons = "; ".join(f"{i.field}: {i.message}" for i in issues)
        annotate_run(conn, run_id, outcome="invalid", error=f"validation: {reasons}")
    elif pay is not None and pay.disagreement:
        annotate_run(conn, run_id, outcome="ok", error=f"pay disagreement: {pay.detail}")


def _pay(
    result: LLMResult[PostingFacts],
    issues: list[Issue],
    ats_ranges: list[SalaryRange] | None,
) -> PayDecision | None:
    """The pay decision, only for a valid answer: comparing unvalidated numbers with the
    ATS would say nothing useful."""
    if result.parsed is None or issues:
        return None
    return decide_pay(result.parsed.salary.ranges, ats_ranges)


def _repair_call(
    first: LLMCall, previous: LLMResult[PostingFacts], problems: list[Issue]
) -> LLMCall:
    """The original call plus the previous answer and the problems, as ONE user message.

    The system prompt is unchanged, so the provider's prompt cache still hits on it.
    """
    if previous.raw_text is not None:
        previous_text = previous.raw_text
    elif previous.parsed is not None:
        previous_text = previous.parsed.model_dump_json()
    else:
        previous_text = "(no output)"
    problem_lines = "\n".join(f"- {issue.field}: {issue.message}" for issue in problems)
    suffix = _REPAIR_TEMPLATE.format(previous=previous_text, problems=problem_lines)
    return LLMCall(
        purpose=first.purpose,
        prompt_name=first.prompt_name,
        prompt_version=first.prompt_version,
        provider=first.provider,
        model=first.model,
        system=first.system,
        user=first.user + suffix,
        max_tokens=first.max_tokens,
        effort=first.effort,
        sensitive=first.sensitive,
    )


def _store(
    conn: psycopg.Connection[Any],
    posting_id: int,
    content_hash: str,
    call: LLMCall,
    result: LLMResult[PostingFacts],
    issues: list[Issue],
    pay: PayDecision | None,
    totals: _Attempts,
    job: queue.Job | None,
) -> None:
    """The state transaction (D-008): facts row, then the fenced job completion."""
    ok = result.parsed is not None and not issues
    # A failed extraction shows no pay at all: failed postings never reach the feed, and
    # a half-trusted display value would be worse than none.
    display: list[SalaryRange] = pay.display_ranges if ok and pay is not None else []
    params = {
        "posting_id": posting_id,
        "status": "ok" if ok else "failed",
        # The model's answer exactly as validated, separate from the displayed pay.
        "facts": Jsonb(result.parsed.model_dump(mode="json")) if ok and result.parsed else None,
        "display_salary": Jsonb([r.model_dump(mode="json") for r in display]),
        "pay_source": pay.pay_source if ok and pay is not None else "none",
        "pay_disagreement": bool(ok and pay is not None and pay.disagreement),
        "issues": Jsonb([{"field": i.field, "message": i.message} for i in issues]),
        "content_hash": content_hash,
        "prompt_name": call.prompt_name,
        "prompt_version": call.prompt_version,
        "model": call.model,
        # The run whose output this row reflects: the last attempt.
        "llm_run_id": totals.run_ids[-1],
    }
    with conn.transaction():
        conn.execute(_UPSERT_FACTS, params)
        if job is not None:
            # Last statement: raises LeaseLost if another worker owns the job now, and
            # the `with` block rolls the facts write back with it.
            queue.complete(conn, job)


def _provider_literal(name: str) -> Literal["anthropic", "openai"]:
    # Prompt frontmatter is plain text; LLMCall wants the closed set.
    if name == "anthropic":
        return "anthropic"
    if name == "openai":
        return "openai"
    raise ValueError(f"prompt names unknown provider {name!r}")
