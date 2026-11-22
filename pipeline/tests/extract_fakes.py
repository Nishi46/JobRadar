"""Shared test doubles for the extraction tests: a scripted Provider and a synthetic posting.

Everything here is made up. The posting text is invented (never commit a real posting:
the content belongs to the employer), and the provider never touches the network.
"""

from collections.abc import Callable
from typing import Any, TypeVar, cast

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from jobradar.facts import (
    LocationsField,
    PostingFacts,
    RemoteField,
    SalaryField,
    SalaryRange,
    SeniorityField,
    SkillCite,
    TrackField,
    VisaField,
    YearsField,
)
from jobradar.llm.base import LLMCall, LLMResult, Outcome

Conn = psycopg.Connection[Any]
T = TypeVar("T", bound=BaseModel)

# Paragraph ids are 1-based, the way clean.py numbers them.
PARAGRAPHS: list[dict[str, Any]] = [
    {"id": 1, "text": "Examplo builds evaluation tooling for teams shipping LLM products."},
    {"id": 2, "text": "You will build retrieval pipelines in Python for our customer-facing app."},
    {"id": 3, "text": "You have 5+ years of experience shipping production software."},
    {
        "id": 4,
        "text": "The base salary range for this role in San Francisco, CA is $180,000 - $220,000.",
    },
    {"id": 5, "text": "This role is hybrid, three days a week in San Francisco. We sponsor visas."},
    {"id": 6, "text": "Nice to have: experience designing offline evals."},
]

# A Lever salaryRange (stored untouched in postings.ats_compensation) that disagrees with
# paragraph 4, and one that agrees with it.
LEVER_PAY_DISAGREES = {
    "min": 250000,
    "max": 300000,
    "currency": "USD",
    "interval": "per-year-salary",
}
LEVER_PAY_AGREES = {"min": 180000, "max": 220000, "currency": "USD", "interval": "per-year-salary"}


def good_facts() -> PostingFacts:
    """Facts that pass every validate_facts rule for PARAGRAPHS."""
    return PostingFacts(
        track=TrackField(value=["product"], evidence=[2]),
        seniority=SeniorityField(value="senior", evidence=[3]),
        years_min=YearsField(value=5, evidence=[3]),
        salary=SalaryField(
            ranges=[
                SalaryRange(
                    location="San Francisco, CA",
                    min=180000,
                    max=220000,
                    currency="USD",
                    pay_type="annual_base",
                )
            ],
            evidence=4,
        ),
        locations=LocationsField(value=["San Francisco, CA"], evidence=[5]),
        remote_policy=RemoteField(value="hybrid", evidence=[5]),
        visa_sponsorship=VisaField(value="yes", evidence=[5]),
        required_skills=[SkillCite(code="PY", evidence=2), SkillCite(code="RAG", evidence=2)],
        preferred_skills=[SkillCite(code="EVALS", evidence=6)],
    )


def bad_facts() -> PostingFacts:
    """Schema-valid but fails validation: a skill code outside the taxonomy."""
    facts = good_facts()
    return facts.model_copy(update={"required_skills": [SkillCite(code="NOT_A_SKILL", evidence=2)]})


def llm_result(
    outcome: Outcome = "ok",
    parsed: BaseModel | None = None,
    *,
    raw_text: str | None = None,
    error: str | None = None,
    retryable: bool = False,
    cost_usd: float | None = 0.001,
    latency_ms: int = 100,
) -> LLMResult[Any]:
    if raw_text is None and parsed is not None:
        raw_text = parsed.model_dump_json()
    return LLMResult(
        parsed=parsed,
        raw_text=raw_text,
        outcome=outcome,
        error=error,
        retryable=retryable,
        provider="anthropic",
        model="claude-haiku-5-5",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )


class FakeProvider:
    """Returns scripted results in order and records every call it was given.

    `on_call` runs before each result is returned, so a test can simulate something
    happening "during" the API call (an ingest editing the posting, say).
    """

    name = "anthropic"

    def __init__(
        self, results: list[LLMResult[Any]], on_call: Callable[[int], None] | None = None
    ) -> None:
        self.results = list(results)
        self.calls: list[LLMCall] = []
        self.on_call = on_call

    def complete(self, call: LLMCall, schema: type[T]) -> LLMResult[T]:
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if not self.results:
            raise AssertionError(f"unexpected LLM call #{len(self.calls)}")
        return cast(LLMResult[T], self.results.pop(0))


def make_board(db: Conn, *, ats: str = "greenhouse", token: str = "examplo") -> int:
    row = db.execute(
        "INSERT INTO boards (ats, board_token, company) VALUES (%s, %s, 'Examplo') RETURNING id",
        (ats, token),
    ).fetchone()
    assert row is not None
    board_id: int = row[0]
    return board_id


def make_posting(
    db: Conn,
    board_id: int,
    *,
    external_id: str = "1",
    content_hash: str = "hash-1",
    ats_compensation: dict[str, Any] | None = None,
    status: str = "open",
    first_seen_offset: str = "0 hours",
) -> int:
    row = db.execute(
        """
        INSERT INTO postings (
            board_id, external_id, url, title, locations_raw, departments,
            description_html, cleaned_text, paragraphs, content_hash, cleaner_version,
            ats_compensation, ats_raw, status, closed_at, first_seen
        ) VALUES (
            %(board_id)s, %(external_id)s, %(url)s, 'Senior Applied AI Engineer',
            ARRAY['San Francisco, CA'], ARRAY['Engineering'], '<p>synthetic</p>',
            %(text)s, %(paragraphs)s, %(hash)s, 1, %(comp)s, '{}'::jsonb, %(status)s,
            CASE WHEN %(status)s = 'closed' THEN now() END,
            now() - %(offset)s::interval
        ) RETURNING id
        """,
        {
            "board_id": board_id,
            "external_id": external_id,
            "url": f"https://jobs.example.com/examplo/{external_id}",
            "text": "\n\n".join(p["text"] for p in PARAGRAPHS),
            "paragraphs": Jsonb(PARAGRAPHS),
            "hash": content_hash,
            "comp": Jsonb(ats_compensation) if ats_compensation is not None else None,
            "status": status,
            "offset": first_seen_offset,
        },
    ).fetchone()
    assert row is not None
    posting_id: int = row[0]
    return posting_id


def facts_row(db: Conn, posting_id: int) -> dict[str, Any] | None:
    with db.cursor(row_factory=psycopg.rows.dict_row) as cur:
        return cur.execute(
            "SELECT * FROM posting_facts WHERE posting_id = %s", (posting_id,)
        ).fetchone()


def scalar(db: Conn, sql: str, *params: Any) -> Any:
    row = db.execute(sql, params).fetchone()
    assert row is not None
    return row[0]
