"""Matching: the D-004 hard filters, the skill-overlap score, and run_match end to end.

The filter tests come first and are pure (no database): one test per rule in
DESIGN.md §4 / DECISIONS.md D-004. The run_match tests use synthetic postings in the
rolled-back `db` fixture and a fake Provider, so no real API is ever called.
"""

import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast

import psycopg
import pytest
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
from jobradar.llm.base import LLMCall, LLMResult
from jobradar.match import (
    CandidateRow,
    RerankItem,
    RerankOutput,
    candidate_dict,
    feed,
    overlap_score,
    passes_filters,
    run_match,
    shown_paragraph_ids,
    stable_shuffle_key,
    stage1,
    track_weight,
    validate_rerank,
)
from jobradar.profiles import MatchFilters, ProfileFacts, create_profile
from jobradar.render import render_candidates

Conn = psycopg.Connection[Any]
T = TypeVar("T", bound=BaseModel)


# --- builders ---------------------------------------------------------------------


def make_facts(
    *,
    tracks: list[str] | None = None,
    seniority: str = "senior",
    locations: list[str] | None = None,
    remote: str = "onsite",
    ranges: list[SalaryRange] | None = None,
    required: list[str] | None = None,
    preferred: list[str] | None = None,
    extra_cites: list[int] | None = None,
) -> PostingFacts:
    """A valid PostingFacts; every field cites paragraph 1. `extra_cites` adds track
    citations, which controls which paragraphs the re-ranker is shown."""
    return PostingFacts.model_validate(
        {
            "track": TrackField.model_validate(
                {
                    "value": tracks if tracks is not None else ["platform"],
                    "evidence": [1, *(extra_cites or [])],
                }
            ),
            "seniority": SeniorityField.model_validate({"value": seniority, "evidence": [1]}),
            "years_min": YearsField(value=None, evidence=[]),
            "salary": SalaryField(ranges=ranges or [], evidence=1 if ranges else None),
            "locations": LocationsField(
                value=locations if locations is not None else ["San Francisco, CA"],
                evidence=[1],
            ),
            "remote_policy": RemoteField.model_validate({"value": remote, "evidence": [1]}),
            "visa_sponsorship": VisaField(value="unstated", evidence=[]),
            "required_skills": [SkillCite(code=c, evidence=1) for c in required or []],
            "preferred_skills": [SkillCite(code=c, evidence=1) for c in preferred or []],
        }
    )


def rng(
    lo: int,
    hi: int,
    location: str = "San Francisco, CA",
    pay_type: str = "annual_base",
    currency: str = "USD",
) -> SalaryRange:
    return SalaryRange.model_validate(
        {"location": location, "min": lo, "max": hi, "currency": currency, "pay_type": pay_type}
    )


def cand(facts: PostingFacts, *, display: list[SalaryRange] | None = None) -> CandidateRow:
    return CandidateRow(
        posting_id=1,
        company="Acme",
        title="Applied AI Engineer",
        url="https://example.com/jobs/1",
        first_seen=datetime(2026, 11, 20, tzinfo=UTC),
        paragraphs=[{"id": 1, "text": "Synthetic paragraph."}],
        facts=facts,
        display_salary=display if display is not None else facts.salary.ranges,
    )


def passes(facts: PostingFacts, **filters: Any) -> bool:
    return passes_filters(cand(facts), MatchFilters.model_validate(filters))


# --- track filter -----------------------------------------------------------------


def test_track_no_user_tracks_means_no_constraint() -> None:
    assert passes(make_facts(tracks=["research"]))


def test_track_passes_when_any_posting_track_matches() -> None:
    assert passes(make_facts(tracks=["research", "product"]), tracks=["product"])


def test_track_fails_when_no_track_matches() -> None:
    assert not passes(make_facts(tracks=["research"]), tracks=["platform", "product"])


def test_track_unknown_passes() -> None:
    assert passes(make_facts(tracks=["unknown"]), tracks=["platform"])


def test_track_other_fails() -> None:
    # `other` is a real non-target role (D-004), unlike `unknown`.
    assert not passes(make_facts(tracks=["other"]), tracks=["platform"])


# --- seniority filter --------------------------------------------------------------


def test_seniority_matches() -> None:
    assert passes(make_facts(seniority="mid"), seniorities=["mid", "senior"])


def test_seniority_mismatch_fails() -> None:
    assert not passes(make_facts(seniority="staff_plus"), seniorities=["mid", "senior"])


def test_seniority_unknown_passes() -> None:
    assert passes(make_facts(seniority="unknown"), seniorities=["mid"])


def test_seniority_no_user_seniorities_means_no_constraint() -> None:
    assert passes(make_facts(seniority="manager"))


# --- location filter ---------------------------------------------------------------


def test_location_case_insensitive_substring_match() -> None:
    assert passes(make_facts(locations=["New York, NY"]), locations=["new york"])


def test_location_substring_works_both_ways() -> None:
    # The user typed more than the posting's normalized string.
    assert passes(make_facts(locations=["New York"]), locations=["New York, NY, USA"])


def test_location_any_of_several_user_locations() -> None:
    assert passes(
        make_facts(locations=["San Francisco, CA"]),
        locations=["New York, NY", "San Francisco"],
    )


def test_location_mismatch_onsite_fails() -> None:
    assert not passes(make_facts(locations=["Austin, TX"]), locations=["New York, NY"])


def test_location_remote_posting_passes_when_remote_ok() -> None:
    assert passes(make_facts(locations=["Austin, TX"], remote="remote"), locations=["New York, NY"])


def test_location_remote_posting_fails_when_not_remote_ok() -> None:
    assert not passes(
        make_facts(locations=["Austin, TX"], remote="remote"),
        locations=["New York, NY"],
        remote_ok=False,
    )


def test_location_remote_location_string_counts_as_remote() -> None:
    # remote_policy can be unknown while a location says "Remote - US".
    assert passes(
        make_facts(locations=["Remote - US"], remote="unknown"), locations=["New York, NY"]
    )


def test_location_unknown_remote_policy_passes_when_remote_ok() -> None:
    # D-004: unknown passes. We can't rule out that the role is remote.
    assert passes(
        make_facts(locations=["Austin, TX"], remote="unknown"), locations=["New York, NY"]
    )


def test_location_unknown_remote_policy_fails_when_not_remote_ok() -> None:
    assert not passes(
        make_facts(locations=["Austin, TX"], remote="unknown"),
        locations=["New York, NY"],
        remote_ok=False,
    )


def test_location_known_onsite_without_match_fails() -> None:
    assert not passes(
        make_facts(locations=["Austin, TX"], remote="onsite"), locations=["New York, NY"]
    )


def test_location_hybrid_is_not_remote() -> None:
    assert not passes(
        make_facts(locations=["Austin, TX"], remote="hybrid"), locations=["New York, NY"]
    )


def test_location_unknown_posting_locations_pass() -> None:
    assert passes(make_facts(locations=[], remote="unknown"), locations=["New York, NY"])


def test_location_no_user_locations_means_no_constraint() -> None:
    assert passes(make_facts(locations=["Austin, TX"]), remote_ok=False)


# --- pay filter --------------------------------------------------------------------


def test_pay_no_minimum_passes() -> None:
    assert passes(make_facts(ranges=[rng(50_000, 60_000)]))


def test_pay_no_ranges_passes() -> None:
    # Pay not published is "unknown" for the pay filter.
    assert passes(make_facts(ranges=[]), min_pay_usd=200_000)


def test_pay_uses_the_range_for_the_users_location() -> None:
    facts = make_facts(
        locations=["San Francisco, CA", "Austin, TX"],
        ranges=[rng(200_000, 250_000, "San Francisco, CA"), rng(140_000, 170_000, "Austin, TX")],
    )
    # An Austin user is judged on the Austin range, not the higher SF one.
    assert not passes(facts, locations=["Austin"], min_pay_usd=180_000)
    assert passes(facts, locations=["San Francisco"], min_pay_usd=180_000)


def test_pay_range_meets_minimum_when_its_top_reaches_it() -> None:
    # Permissive: the posting *could* pay the minimum.
    assert passes(make_facts(ranges=[rng(150_000, 200_000)]), min_pay_usd=190_000)
    assert not passes(make_facts(ranges=[rng(150_000, 180_000)]), min_pay_usd=190_000)


def test_pay_no_matching_range_falls_back_to_the_highest() -> None:
    facts = make_facts(
        locations=["San Francisco, CA", "Austin, TX", "Remote - US"],
        remote="remote",
        ranges=[rng(200_000, 250_000, "San Francisco, CA"), rng(140_000, 170_000, "Austin, TX")],
    )
    assert passes(facts, locations=["Denver, CO"], min_pay_usd=240_000)
    assert not passes(facts, locations=["Denver, CO"], min_pay_usd=260_000)


def test_pay_any_matching_range_across_user_locations() -> None:
    facts = make_facts(
        locations=["San Francisco, CA", "Austin, TX"],
        ranges=[rng(200_000, 250_000, "San Francisco, CA"), rng(140_000, 170_000, "Austin, TX")],
    )
    assert passes(facts, locations=["Austin", "San Francisco"], min_pay_usd=240_000)


def test_pay_hourly_is_annualized_at_2080_hours() -> None:
    hourly = [rng(80, 100, pay_type="hourly")]  # top = 208,000 / year
    assert passes(make_facts(ranges=hourly), min_pay_usd=208_000)
    assert not passes(make_facts(ranges=hourly), min_pay_usd=208_001)


def test_pay_ote_is_compared_as_is() -> None:
    ote = [rng(150_000, 220_000, pay_type="ote")]
    assert passes(make_facts(ranges=ote), min_pay_usd=220_000)
    assert not passes(make_facts(ranges=ote), min_pay_usd=220_001)


def test_pay_location_less_ats_range_never_matches_but_counts_as_highest() -> None:
    # Ashby tiers carry no location (""): an empty string must not "match" everything.
    facts = make_facts(ranges=[rng(150_000, 170_000, ""), rng(200_000, 230_000, "Austin, TX")])
    assert not passes(facts, locations=["Austin"], min_pay_usd=180_000)


def test_pay_non_usd_ranges_are_not_compared() -> None:
    # min_pay_usd can't be compared against EUR without an exchange rate: unknown → pass.
    assert passes(make_facts(ranges=[rng(60_000, 80_000, currency="EUR")]), min_pay_usd=200_000)


def test_pay_uses_display_salary_not_raw_llm_ranges() -> None:
    # The ATS value wins when they disagree (DESIGN §5), and that's what the user sees.
    facts = make_facts(ranges=[rng(250_000, 300_000)])
    row = cand(facts, display=[rng(120_000, 140_000)])
    assert not passes_filters(row, MatchFilters(min_pay_usd=200_000))


# --- overlap score -----------------------------------------------------------------


def test_overlap_required_weighs_double_preferred() -> None:
    # (1 + 0.5·1) / (2 + 0.5·2 + 2 smoothing) = 1.5 / 5
    score = overlap_score({"PY", "RAG"}, required={"PY", "EVALS"}, preferred={"RAG", "MCP"})
    assert score == pytest.approx(0.3)


def test_overlap_no_posting_skills_is_zero() -> None:
    assert overlap_score({"PY"}, required=set(), preferred=set()) == 0.0


def test_overlap_more_matched_evidence_beats_a_short_list() -> None:
    # The live bug: with 23 profile skills, 1,264 postings tied at 1.0, most of them
    # asking for a single skill. Smoothing makes 6 of 6 outrank 1 of 1.
    six = {"PY", "TS", "SQL", "EVALS", "RAG", "MCP"}
    assert overlap_score(six, required=six, preferred=set()) > overlap_score(
        six, required={"PY"}, preferred=set()
    )
    assert overlap_score({"PY"}, required={"PY"}, preferred=set()) == pytest.approx(1 / 3)


def test_overlap_is_normalized_by_the_posting_not_the_profile() -> None:
    # Extra profile skills don't lower the score.
    assert overlap_score({"PY"}, {"PY", "TS"}, set()) == overlap_score(
        {"PY", "A", "B", "C"}, {"PY", "TS"}, set()
    )


def test_overlap_never_reaches_one() -> None:
    many = {f"S{i}" for i in range(30)}
    assert overlap_score(many, required=many, preferred=set()) < 1.0


# --- track weight ------------------------------------------------------------------


def test_track_weight_without_profile_tracks_is_neutral() -> None:
    assert track_weight(["other"], []) == 1.0


def test_track_weight_ordering() -> None:
    mine = ["platform", "product"]
    match = track_weight(["research", "platform"], mine)
    unknown = track_weight(["unknown"], mine)
    other_ai = track_weight(["research"], mine)
    other = track_weight(["other"], mine)
    assert match == 1.0
    assert match > unknown > other_ai > other > 0  # `other` is demoted, never removed


def test_stage1_demotes_other_track_below_a_weaker_skill_match() -> None:
    profile = ProfileFacts(
        skills=["PY", "TS", "SQL"], tracks=["platform"], seniority="mid", years_experience=2
    )
    sales = row(1, make_facts(tracks=["other"], required=["PY", "TS", "SQL"]))
    platform = row(2, make_facts(tracks=["platform"], required=["PY", "GO"]))

    ranked, passed = stage1([sales, platform], profile, MatchFilters(), top_k=10)

    assert [r.posting_id for r, _ in ranked] == [2, 1]
    assert passed == 2


# --- stage-1 tie-breaks ------------------------------------------------------------


def row(
    posting_id: int, facts: PostingFacts, *, first_seen: datetime | None = None
) -> CandidateRow:
    return CandidateRow(
        posting_id=posting_id,
        company=f"Company {posting_id}",
        title="Engineer",
        url=f"https://example.com/jobs/{posting_id}",
        first_seen=first_seen or datetime(2026, 11, 21, 23, 45, tzinfo=UTC),
        paragraphs=[{"id": 1, "text": "Synthetic paragraph."}],
        facts=facts,
        display_salary=facts.salary.ranges,
    )


def test_ties_on_the_same_day_ignore_seconds_and_use_the_stable_shuffle() -> None:
    # A bulk ingest stamps every posting within minutes; seconds must not decide the
    # order (that sorted the live feed by company name).
    base = datetime(2026, 11, 21, 23, 45, tzinfo=UTC)
    rows = [
        row(i, make_facts(required=["PY"]), first_seen=base + timedelta(seconds=i))
        for i in range(1, 21)
    ]

    ranked, _ = stage1(rows, PROFILE, MatchFilters(), top_k=20)

    assert [r.posting_id for r, _ in ranked] == sorted(range(1, 21), key=stable_shuffle_key)


def test_ties_prefer_a_newer_day() -> None:
    old = row(1, make_facts(required=["PY"]), first_seen=datetime(2026, 11, 1, tzinfo=UTC))
    new = row(2, make_facts(required=["PY"]), first_seen=datetime(2026, 11, 20, tzinfo=UTC))

    ranked, _ = stage1([old, new], PROFILE, MatchFilters(), top_k=2)

    assert [r.posting_id for r, _ in ranked] == [2, 1]


def test_stable_shuffle_is_deterministic_and_not_id_order() -> None:
    ids = list(range(1, 50))
    assert sorted(ids, key=stable_shuffle_key) == sorted(ids, key=stable_shuffle_key)
    assert sorted(ids, key=stable_shuffle_key) != ids


# --- rerank validation (pure) ------------------------------------------------------


def _cands(*ids: int, facts: PostingFacts | None = None) -> dict[int, CandidateRow]:
    # Paragraphs 1-3 exist; the default facts cite only 1, so only [1] is shown.
    paragraphs = [{"id": n, "text": f"p{n}"} for n in (1, 2, 3)]
    f = facts or make_facts()
    return {i: replace(cand(f), posting_id=i, paragraphs=paragraphs) for i in ids}


def item(pid: int, **kw: Any) -> RerankItem:
    fields: dict[str, Any] = {
        "posting_id": pid,
        "why": "Fits.",
        "evidence": [1],
        "skills_to_learn": [],
    }
    return RerankItem.model_validate(fields | kw)


def test_validate_rerank_keeps_valid_items_in_order() -> None:
    kept, problems = validate_rerank(RerankOutput(matches=[item(2), item(1)]), _cands(1, 2))
    assert [i.posting_id for i in kept] == [2, 1]
    assert problems == []


@pytest.mark.parametrize(
    "bad",
    [
        item(99),  # not a candidate
        item(1, evidence=[7]),  # paragraph doesn't exist
        item(1, evidence=[]),  # ungrounded
        item(1, why="  "),  # no explanation
        item(1, evidence=[2]),  # exists in the posting but wasn't shown to the model
    ],
)
def test_validate_rerank_drops_invalid_items(bad: RerankItem) -> None:
    kept, problems = validate_rerank(RerankOutput(matches=[bad, item(2)]), _cands(1, 2))
    assert [i.posting_id for i in kept] == [2]
    assert len(problems) == 1


def test_validate_rerank_accepts_any_shown_paragraph() -> None:
    cands = _cands(1, facts=make_facts(extra_cites=[3]))
    kept, problems = validate_rerank(RerankOutput(matches=[item(1, evidence=[1, 3])]), cands)
    assert [i.evidence for i in kept] == [[1, 3]]
    assert problems == []


def test_validate_rerank_never_accepts_header_paragraph_zero() -> None:
    # Facts may cite 0 (a derived header line), but 0 isn't in postings.paragraphs, so the
    # re-ranker never saw it as a numbered paragraph.
    cands = _cands(1, facts=make_facts(extra_cites=[0]))
    kept, problems = validate_rerank(RerankOutput(matches=[item(1, evidence=[0])]), cands)
    assert kept == []
    assert len(problems) == 1


def test_shown_paragraph_ids_agree_with_render_candidates() -> None:
    # validate_rerank recomputes the shown set; it must match what render actually showed.
    row = next(iter(_cands(1, facts=make_facts(extra_cites=[0, 3])).values()))
    rendered = render_candidates([candidate_dict(row)])
    in_prompt = {int(m) for m in re.findall(r"^\[(\d+)\] ", rendered, flags=re.M)}
    assert shown_paragraph_ids(row) == in_prompt == {1, 3}


def test_validate_rerank_drops_only_bad_skill_codes(caplog: pytest.LogCaptureFixture) -> None:
    out = RerankOutput(matches=[item(1, skills_to_learn=["TS", "NOT_A_CODE", "RAG"])])
    with caplog.at_level(logging.WARNING):
        kept, problems = validate_rerank(out, _cands(1))
    assert [i.skills_to_learn for i in kept] == [["TS", "RAG"]]
    assert problems == []
    assert "dropped 1 skills_to_learn code" in caplog.text


def test_validate_rerank_keeps_only_the_first_copy_of_a_posting() -> None:
    kept, problems = validate_rerank(
        RerankOutput(matches=[item(1), item(2), item(1, why="Again.")]), _cands(1, 2)
    )
    assert [(i.posting_id, i.why) for i in kept] == [(1, "Fits."), (2, "Fits.")]
    assert len(problems) == 1


# --- run_match against Postgres ----------------------------------------------------


class FakeProvider:
    """Returns scripted results and remembers every call it was given."""

    name = "openai"

    def __init__(self, *results: LLMResult[Any]) -> None:
        self.results = list(results)
        self.calls: list[LLMCall] = []

    def complete(self, call: LLMCall, schema: type[T]) -> LLMResult[T]:
        self.calls.append(call)
        return cast(LLMResult[T], self.results.pop(0))


def llm_result(parsed: BaseModel | None, outcome: str = "ok") -> LLMResult[Any]:
    return LLMResult(
        parsed=parsed,
        raw_text=parsed.model_dump_json() if parsed is not None else None,
        outcome=outcome,  # type: ignore[arg-type]
        error=None if outcome == "ok" else outcome,
        retryable=False,
        provider="openai",
        model="gpt-6.2-sol",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=1234,
        cost_usd=0.004,
    )


PROFILE = ProfileFacts(
    skills=["PY", "EVALS", "RAG"], tracks=["platform"], seniority="senior", years_experience=5
)


@pytest.fixture
def board(db: Conn, empty_tables: None) -> int:
    row = db.execute(
        "INSERT INTO boards (ats, board_token, company) VALUES ('greenhouse', 'acme', 'Acme')"
        " RETURNING id"
    ).fetchone()
    assert row is not None
    board_id: int = row[0]
    return board_id


def add_posting(
    db: Conn,
    board_id: int,
    ext: str,
    facts: PostingFacts,
    *,
    age_days: float = 1,
    status: str = "open",
    facts_status: str = "ok",
    stale: bool = False,
) -> int:
    """A synthetic open posting with extracted facts. `stale` = facts from an older hash."""
    first_seen = datetime.now(UTC) - timedelta(days=age_days)
    row = db.execute(
        """
        INSERT INTO postings (board_id, external_id, url, title, description_html, cleaned_text,
                              paragraphs, content_hash, cleaner_version, ats_raw, status,
                              first_seen, closed_at)
        VALUES (%s, %s, %s, %s, '<p>x</p>', 'x', %s, %s, 1, '{}', %s, %s, %s)
        RETURNING id
        """,
        (
            board_id,
            ext,
            f"https://example.com/jobs/{ext}",
            f"Engineer {ext}",
            Jsonb([{"id": 1, "text": "Synthetic one."}, {"id": 2, "text": "Synthetic two."}]),
            f"hash-{ext}",
            status,
            first_seen,
            first_seen if status == "closed" else None,
        ),
    ).fetchone()
    assert row is not None
    posting_id: int = row[0]
    db.execute(
        """
        INSERT INTO posting_facts (posting_id, status, facts, display_salary, pay_source,
                                   content_hash, prompt_name, prompt_version, model)
        VALUES (%s, %s, %s, %s, %s, %s, 'extract_posting', 1, 'claude-haiku-5-5')
        """,
        (
            posting_id,
            facts_status,
            Jsonb(facts.model_dump()) if facts_status == "ok" else None,
            Jsonb([r.model_dump() for r in facts.salary.ranges]),
            "text" if facts.salary.ranges else "none",
            "old-hash" if stale else f"hash-{ext}",
        ),
    )
    return posting_id


def make_profile(db: Conn, filters: MatchFilters | None = None) -> None:
    create_profile(db, "tester", PROFILE, filters or MatchFilters(), resume_text=None)


def matches(db: Conn, run_id: int) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT posting_id, stage1_rank, final_rank, why, why_evidence, skills_to_learn"
        " FROM matches WHERE match_run_id = %s ORDER BY final_rank",
        (run_id,),
    ).fetchall()


def run_row(db: Conn, run_id: int) -> tuple[Any, ...]:
    row = db.execute(
        "SELECT candidates, passed_filters, reranked, status FROM match_runs WHERE id = %s",
        (run_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_run_match_filters_scores_reranks_and_writes_the_feed(db: Conn, board: int) -> None:
    make_profile(db, MatchFilters(tracks=["platform"]))
    good = add_posting(db, board, "good", make_facts(required=["PY", "EVALS"]))
    ok = add_posting(db, board, "ok", make_facts(required=["PY", "TS"], extra_cites=[2]))
    add_posting(db, board, "research", make_facts(tracks=["research"], required=["PY"]))
    # The re-ranker prefers `ok` and says the user would need TypeScript for it.
    output = RerankOutput(
        matches=[
            item(ok, why="Platform role using Python.", evidence=[2], skills_to_learn=["TS"]),
            item(good, why="Evals-heavy platform work.", evidence=[1]),
        ]
    )
    provider = FakeProvider(llm_result(output))

    run_id = run_match(db, "tester", provider=provider)

    assert run_row(db, run_id) == (3, 2, 2, "ok")
    rows = matches(db, run_id)
    # stage-1: good (2 of 2) before ok (1 of 2); final: the re-ranker's order.
    assert [(r[0], r[1], r[2]) for r in rows] == [(ok, 2, 1), (good, 1, 2)]
    assert rows[0][3:] == ("Platform role using Python.", [2], ["TS"])

    call = provider.calls[0]
    assert call.purpose == "rerank"
    assert call.sensitive  # the profile is personal data (CLAUDE.md): never traced
    logged = db.execute(
        "SELECT purpose, match_run_id, output FROM llm_runs WHERE match_run_id = %s", (run_id,)
    ).fetchall()
    assert logged == [("rerank", run_id, None)]

    shown = feed(db, run_id)
    assert [r["posting_id"] for r in shown] == [ok, good]
    assert shown[0]["why"] == "Platform role using Python."
    assert shown[0]["url"] == "https://example.com/jobs/ok"
    assert shown[0]["company"] == "Acme"
    assert shown[0]["track"] == ["platform"]
    assert "cleaned_text" not in shown[0]  # never full descriptions


def test_run_match_only_considers_fresh_open_recent_ok_facts(db: Conn, board: int) -> None:
    make_profile(db)
    fresh = add_posting(db, board, "fresh", make_facts(required=["PY"]))
    add_posting(db, board, "stale", make_facts(), stale=True)  # posting edited since
    add_posting(db, board, "failed", make_facts(), facts_status="failed")
    add_posting(db, board, "closed", make_facts(), status="closed")
    add_posting(db, board, "old", make_facts(), age_days=40)
    provider = FakeProvider(llm_result(RerankOutput(matches=[item(fresh)])))

    run_id = run_match(db, "tester", provider=provider, since_days=30)

    assert run_row(db, run_id)[:2] == (1, 1)
    assert [r[0] for r in matches(db, run_id)] == [fresh]


def test_run_match_top_k_ties_break_newer_first(db: Conn, board: int) -> None:
    make_profile(db)
    older = add_posting(db, board, "older", make_facts(required=["PY"]), age_days=5)
    newer = add_posting(db, board, "newer", make_facts(required=["PY"]), age_days=1)
    add_posting(db, board, "worse", make_facts(required=["PY", "TS"]), age_days=0.5)
    provider = FakeProvider(llm_result(RerankOutput(matches=[])))

    run_id = run_match(db, "tester", provider=provider, top_k=2)

    rows = matches(db, run_id)
    assert [(r[0], r[1]) for r in rows] == [(newer, 1), (older, 2)]
    # Only the top_k go to the re-ranker.
    assert run_row(db, run_id)[1] == 3


def test_run_match_partial_rerank_appends_missing_in_stage1_order(db: Conn, board: int) -> None:
    make_profile(db)
    a = add_posting(db, board, "a", make_facts(required=["PY"]))
    b = add_posting(db, board, "b", make_facts(required=["PY", "EVALS", "TS"]))
    c = add_posting(db, board, "c", make_facts(required=["PY", "TS"]))
    # Stage 1 (smoothed, D-022): b (2/5 = 0.40), a (1/3 = 0.33), c (1/4 = 0.25). The model
    # returns c, a bogus id, and a bad citation for a: c is kept, then the rest follow in
    # stage-1 order.
    output = RerankOutput(matches=[item(c), item(999), item(a, evidence=[42])])
    provider = FakeProvider(llm_result(output))

    run_id = run_match(db, "tester", provider=provider)

    rows = matches(db, run_id)
    assert [(r[0], r[2]) for r in rows] == [(c, 1), (b, 2), (a, 3)]
    assert rows[2][3] is None  # dropped items get no "why"
    assert run_row(db, run_id)[2:] == (1, "ok")


@pytest.mark.parametrize(
    "result",
    [
        llm_result(None, outcome="refused"),
        llm_result(None, outcome="api_error"),
        llm_result(RerankOutput(matches=[item(999)])),  # nothing valid
    ],
)
def test_run_match_rerank_failure_falls_back_to_stage1(
    db: Conn, board: int, result: LLMResult[Any]
) -> None:
    make_profile(db)
    a = add_posting(db, board, "a", make_facts(required=["PY"]))
    b = add_posting(db, board, "b", make_facts(required=["PY", "TS"]))

    run_id = run_match(db, "tester", provider=FakeProvider(result))

    assert run_row(db, run_id)[2:] == (0, "rerank_failed")
    assert [(r[0], r[1], r[2], r[3]) for r in matches(db, run_id)] == [
        (a, 1, 1, None),
        (b, 2, 2, None),
    ]
    # The failed call is still logged: it cost money.
    assert db.execute(
        "SELECT count(*) FROM llm_runs WHERE match_run_id = %s", (run_id,)
    ).fetchone() == (1,)


def test_run_match_with_no_candidates_skips_the_llm(db: Conn, board: int) -> None:
    make_profile(db, MatchFilters(tracks=["product"]))
    add_posting(db, board, "a", make_facts(tracks=["platform"]))
    provider = FakeProvider()  # any call would pop from an empty list and fail

    run_id = run_match(db, "tester", provider=provider)

    assert run_row(db, run_id) == (1, 0, 0, "ok")
    assert provider.calls == []
    assert feed(db, run_id) == []
