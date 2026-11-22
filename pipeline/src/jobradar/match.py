"""Two-stage matching: code filters and scores, then a stronger model re-ranks the top k.

Stage 1 is all code (DESIGN.md §4), because the filtered fields are hard constraints and
skill overlap is set arithmetic:

1. **Candidates**: open postings whose extracted facts are `ok` and *fresh* (extracted
   from the posting's current content_hash; stale facts describe text that no longer
   exists), first seen within `since_days`.
2. **Filters** (`passes_filters`): permissive by design (D-004). A posting wrongly
   filtered out is a miss nobody ever sees; a borderline one that gets through is
   visible and becomes feedback. So `unknown` passes, and the pay filter uses the most
   generous reading that's still honest.
3. **Score** (`overlap_score`), then the top k by score, newer first on ties.

Stage 2 sends those k to the re-rank model, which orders them and writes a one-sentence
"why" citing paragraph ids plus "skills to learn". Its output is validated item by item
(`validate_rerank`); invalid items are dropped rather than failing the run, and any
candidate the model left out keeps its stage-1 order after the re-ranked ones.

Write order, so a crash never leaves a run that lies: the `match_runs` row and the
`matches` rows are first written with status `rerank_failed` and final order = stage-1
order (exactly what the feed falls back to). Only after a valid re-rank does a second
transaction update the ranks and flip the status to `ok`. The llm_runs row is recorded
in between, in its own transaction (D-009), and needs the match_runs id to exist first.

The re-rank input contains the user's profile facts, which are personal data
(CLAUDE.md), so the call is `sensitive`: no input/output in traces or llm_runs. The
model's output that matters (why, skills) is stored in `matches` anyway.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ValidationError

from jobradar.db import connect
from jobradar.facts import PostingFacts, SalaryRange
from jobradar.llm.base import LLMCall, LLMResult, Provider
from jobradar.llm.registry import get_provider
from jobradar.llm_runs import record_run
from jobradar.profiles import MatchFilters, ProfileFacts, ProfileNotFoundError, get_profile
from jobradar.prompts import load_prompt
from jobradar.render import render_candidates, render_profile, render_taxonomy
from jobradar.taxonomy import SKILL_CODES
from jobradar.tracing import init_tracing, llm_span

log = logging.getLogger(__name__)

PROMPT_NAME = "rerank"
HOURS_PER_YEAR = 2080  # 40 h x 52 weeks (D-004)
# Preferred skills count half as much as required ones in the overlap score.
PREFERRED_WEIGHT = 0.5


class RerankItem(BaseModel):
    posting_id: int
    why: str
    evidence: list[int]  # paragraph ids of *this* posting
    skills_to_learn: list[str]


class RerankOutput(BaseModel):
    matches: list[RerankItem]  # best first


@dataclass(frozen=True)
class CandidateRow:
    """One posting with fresh, ok facts: everything stage 1 and the re-rank prompt need."""

    posting_id: int
    company: str
    title: str
    url: str
    first_seen: datetime
    paragraphs: list[dict[str, Any]]
    facts: PostingFacts
    # What users see (ATS pay when published, else the LLM's; DESIGN §5). The pay filter
    # uses this, not facts.salary, so the filter agrees with the card.
    display_salary: list[SalaryRange]


# --- stage 1: filters ----------------------------------------------------------------


def _loc_match(a: str, b: str) -> bool:
    """Case-insensitive substring match in either direction ("New York" ~ "New York, NY").
    Empty strings never match: "" is a substring of everything, and Ashby pay tiers
    carry location "" (validate.ats_salary_ranges)."""
    a, b = a.strip().casefold(), b.strip().casefold()
    return bool(a) and bool(b) and (a in b or b in a)


def _track_passes(facts: PostingFacts, filters: MatchFilters) -> bool:
    if not filters.tracks:
        return True
    tracks = facts.track.value
    # `unknown` passes (D-004); `other` only passes if the user asked for it, which in
    # practice means it fails: it is a real, non-target role.
    return "unknown" in tracks or any(t in filters.tracks for t in tracks)


def _seniority_passes(facts: PostingFacts, filters: MatchFilters) -> bool:
    if not filters.seniorities:
        return True
    value = facts.seniority.value
    return value == "unknown" or value in filters.seniorities


def _is_remote(facts: PostingFacts) -> bool:
    # A "Remote - US" location counts even when remote_policy came back unknown.
    return facts.remote_policy.value == "remote" or any(
        "remote" in loc.casefold() for loc in facts.locations.value
    )


def _location_passes(facts: PostingFacts, filters: MatchFilters) -> bool:
    if not filters.locations:
        return True  # no location constraint set
    posting_locations = facts.locations.value
    if not posting_locations:
        return True  # unknown passes
    if any(_loc_match(p, u) for p in posting_locations for u in filters.locations):
        return True
    # No location match: a remote role still fits a remote_ok user, and so does an
    # `unknown` remote policy, since we can't rule remote out (D-004: unknown passes).
    # Only a known onsite/hybrid policy fails here.
    return filters.remote_ok and (_is_remote(facts) or facts.remote_policy.value == "unknown")


def _annual_top(r: SalaryRange) -> int:
    # OTE is compared as is (D-004); only hourly needs converting.
    return r.max * HOURS_PER_YEAR if r.pay_type == "hourly" else r.max


def _pay_passes(ranges: list[SalaryRange], filters: MatchFilters) -> bool:
    """A range "meets the minimum" when its top reaches it: the posting *could* pay that
    much, which is the permissive reading (D-004)."""
    if filters.min_pay_usd is None:
        return True
    # min_pay_usd can't be compared with other currencies without exchange rates, so a
    # non-USD range is treated like unpublished pay: it doesn't filter.
    usd = [r for r in ranges if r.currency == "USD"]
    if not usd:
        return True
    matching = [r for r in usd if any(_loc_match(r.location, u) for u in filters.locations)]
    # No range for the user's location → the highest range is the most the posting
    # could plausibly pay them (D-004); that only drops postings below the minimum
    # under every published range.
    applicable = matching or [max(usd, key=_annual_top)]
    return any(_annual_top(r) >= filters.min_pay_usd for r in applicable)


def passes_filters(row: CandidateRow, filters: MatchFilters) -> bool:
    """All four hard filters of DESIGN §4 / D-004. Each one lets `unknown` through."""
    return (
        _track_passes(row.facts, filters)
        and _seniority_passes(row.facts, filters)
        and _location_passes(row.facts, filters)
        and _pay_passes(row.display_salary, filters)
    )


# --- stage 1: score ------------------------------------------------------------------


def overlap_score(profile_skills: set[str], required: set[str], preferred: set[str]) -> float:
    """Weighted share of the posting's skills that the profile has, in 0..1.

    Required skills weigh 1 and preferred 0.5, because missing a requirement matters more
    than missing a nice-to-have. The denominator is the *posting's* weighted skill count,
    not the profile's: a profile with many extra skills isn't penalized, and the score
    reads as "how much of what they ask for do I have". A posting with no skills scores
    0: there's no evidence of fit, so it sorts below any real overlap (it can still be
    re-ranked up if it makes the top k).
    """
    denominator = len(required) + PREFERRED_WEIGHT * len(preferred)
    if denominator == 0:
        return 0.0
    hits = len(required & profile_skills) + PREFERRED_WEIGHT * len(preferred & profile_skills)
    return hits / denominator


def _score(row: CandidateRow, skills: set[str]) -> float:
    return overlap_score(
        skills,
        required={s.code for s in row.facts.required_skills},
        preferred={s.code for s in row.facts.preferred_skills},
    )


def stage1(
    rows: list[CandidateRow], facts: ProfileFacts, filters: MatchFilters, top_k: int
) -> tuple[list[tuple[CandidateRow, float]], int]:
    """Filter, score and keep the top k. Returns (ranked rows with scores, passed count).
    Ties go to the newer posting (fresher roles are more actionable), then lower id so
    the order is deterministic."""
    skills = set(facts.skills)
    passed = [(r, _score(r, skills)) for r in rows if passes_filters(r, filters)]
    passed.sort(key=lambda rs: (-rs[1], -rs[0].first_seen.timestamp(), rs[0].posting_id))
    return passed[:top_k], len(passed)


# --- stage 2: re-rank validation -----------------------------------------------------


def shown_paragraph_ids(row: CandidateRow) -> set[int]:
    """The paragraph ids render_candidates shows the re-ranker for this posting: those
    its facts cite, limited to ids that exist in postings.paragraphs. Facts may cite 0
    (a derived header line that isn't a stored paragraph), so 0 is never in this set.

    Mirrors render._cited_ids on the typed facts; a test renders a candidate and checks
    the two agree, so a change to what render shows can't silently loosen this check.
    """
    f = row.facts
    cited: set[int] = set()
    for field in (
        f.track,
        f.seniority,
        f.years_min,
        f.locations,
        f.remote_policy,
        f.visa_sponsorship,
    ):
        cited.update(field.evidence)
    if f.salary.evidence is not None:
        cited.add(f.salary.evidence)
    cited.update(s.evidence for s in (*f.required_skills, *f.preferred_skills))
    return {p["id"] for p in row.paragraphs} & cited


def validate_rerank(
    output: RerankOutput, candidates: dict[int, CandidateRow]
) -> tuple[list[RerankItem], list[str]]:
    """Keep the items code can verify; return them (model order) and one problem string
    per dropped item.

    An item is dropped if its posting isn't a candidate or was already ranked, its "why"
    is blank, or it cites no paragraph or one the model wasn't shown for that posting
    (a citation the model couldn't have read isn't grounded). A `skills_to_learn` code
    outside the taxonomy only removes that code: the ranking and the why are still
    usable, and losing a whole item for one bad code would cost more than it protects.
    """
    kept: list[RerankItem] = []
    problems: list[str] = []
    seen: set[int] = set()
    dropped_codes = 0
    for i, item in enumerate(output.matches):
        row = candidates.get(item.posting_id)
        if row is None:
            problem = f"posting_id {item.posting_id} is not a candidate"
        elif item.posting_id in seen:
            problem = f"posting_id {item.posting_id} ranked twice"
        elif not item.why.strip():
            problem = "empty why"
        elif not item.evidence:
            problem = "no evidence cited"
        elif unseen := sorted(set(item.evidence) - shown_paragraph_ids(row)):
            problem = f"evidence {unseen} not shown for posting {item.posting_id}"
        else:
            skills = [c for c in dict.fromkeys(item.skills_to_learn) if c in SKILL_CODES]
            dropped_codes += len(set(item.skills_to_learn) - SKILL_CODES)
            seen.add(item.posting_id)
            kept.append(item.model_copy(update={"skills_to_learn": skills}))
            continue
        problems.append(f"matches[{i}]: {problem}")
    if dropped_codes:
        log.warning("rerank: dropped %d skills_to_learn code(s) not in the taxonomy", dropped_codes)
    return kept, problems


# --- SQL -----------------------------------------------------------------------------

# Fresh facts only: f.content_hash = p.content_hash. now() is the transaction's start
# time, which is fine for a window measured in days.
_CANDIDATES = """
    SELECT p.id, b.company, p.title, p.url, p.first_seen, p.paragraphs,
           f.facts, f.display_salary
    FROM postings p
    JOIN boards b        ON b.id = p.board_id
    JOIN posting_facts f ON f.posting_id = p.id
    WHERE p.status = 'open'
      AND f.status = 'ok'
      AND f.content_hash = p.content_hash
      AND p.first_seen >= now() - make_interval(days => %(since_days)s)
"""

_INSERT_RUN = """
    INSERT INTO match_runs (profile_id, candidates, passed_filters, reranked, status)
    VALUES (%(profile_id)s, %(candidates)s, %(passed)s, 0, %(status)s)
    RETURNING id
"""

# Written as the stage-1 fallback: final_rank = stage1_rank until a valid re-rank lands.
_INSERT_MATCH = """
    INSERT INTO matches (match_run_id, posting_id, stage1_rank, overlap_score, final_rank)
    VALUES (%(run_id)s, %(posting_id)s, %(rank)s, %(score)s, %(rank)s)
"""

_UPDATE_MATCH = """
    UPDATE matches SET final_rank = %(final_rank)s, why = %(why)s,
                       why_evidence = %(evidence)s, skills_to_learn = %(skills)s
    WHERE match_run_id = %(run_id)s AND posting_id = %(posting_id)s
"""

_MARK_RERANKED = "UPDATE match_runs SET status = 'ok', reranked = %s WHERE id = %s"

# The current facts are shown (re-extraction may have refreshed them since the run);
# never cleaned_text or description_html: link to the original instead (CLAUDE.md).
_FEED = """
    SELECT m.final_rank, m.stage1_rank, m.overlap_score, m.why, m.why_evidence,
           m.skills_to_learn, p.id AS posting_id, b.company, p.title, p.url,
           f.display_salary,
           f.facts -> 'track' -> 'value'         AS track,
           f.facts -> 'seniority' ->> 'value'    AS seniority,
           f.facts -> 'remote_policy' ->> 'value' AS remote_policy
    FROM matches m
    JOIN postings p           ON p.id = m.posting_id
    JOIN boards b             ON b.id = p.board_id
    LEFT JOIN posting_facts f ON f.posting_id = p.id
    WHERE m.match_run_id = %s
    ORDER BY m.final_rank NULLS LAST, m.stage1_rank
"""


def load_candidates(conn: psycopg.Connection[Any], since_days: int) -> list[CandidateRow]:
    rows: list[CandidateRow] = []
    for pid, company, title, url, first_seen, paragraphs, facts, display in conn.execute(
        _CANDIDATES, {"since_days": since_days}
    ):
        try:
            rows.append(
                CandidateRow(
                    posting_id=pid,
                    company=company,
                    title=title,
                    url=url,
                    first_seen=first_seen,
                    paragraphs=paragraphs,
                    facts=PostingFacts.model_validate(facts),
                    display_salary=[SalaryRange.model_validate(r) for r in display],
                )
            )
        except ValidationError as e:
            # Facts were validated when stored, so this means the schema moved on since.
            # Skip the posting rather than fail the whole run; re-extraction fixes it.
            log.warning("posting %s: stored facts no longer parse, skipped: %s", pid, e)
    return rows


# --- stage 2: re-rank call -----------------------------------------------------------


def candidate_dict(row: CandidateRow) -> dict[str, Any]:
    """The shape render.render_candidates reads."""
    return {
        "posting_id": row.posting_id,
        "company": row.company,
        "title": row.title,
        "display_salary": [r.model_dump() for r in row.display_salary],
        "facts": row.facts.model_dump(),
        "paragraphs": row.paragraphs,
    }


def _rerank(
    conn: psycopg.Connection[Any],
    provider: Provider | None,
    run_id: int,
    facts: ProfileFacts,
    filters: MatchFilters,
    top: list[CandidateRow],
) -> LLMResult[RerankOutput]:
    prompt = load_prompt(PROMPT_NAME)
    system, user = prompt.render(
        profile=render_profile(facts, filters),
        candidates=render_candidates([candidate_dict(r) for r in top]),
        taxonomy=render_taxonomy(),
    )
    call = LLMCall(
        purpose="rerank",
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        provider=prompt.provider,  # type: ignore[arg-type]  # prompts.py validates it
        model=prompt.model,
        system=system,
        user=user,
        max_tokens=prompt.max_tokens,
        effort=prompt.effort,
        sensitive=True,  # contains the user's profile
    )
    provider = provider or get_provider(prompt.provider)
    with llm_span(call) as span:
        result = provider.complete(call, RerankOutput)
        span.set_result(result)
    record_run(conn, call, result, match_run_id=run_id, trace_id=span.trace_id)
    return result


def run_match(
    conn: psycopg.Connection[Any],
    profile_name: str,
    *,
    provider: Provider | None = None,
    top_k: int = 20,
    since_days: int = 30,
) -> int:
    """Run both stages for one profile and store the result. Returns the match_run id."""
    profile_id, facts, filters = get_profile(conn, profile_name)
    rows = load_candidates(conn, since_days)
    ranked, passed = stage1(rows, facts, filters, top_k)
    top = [r for r, _ in ranked]

    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _INSERT_RUN,
            {
                "profile_id": profile_id,
                "candidates": len(rows),
                "passed": passed,
                # Nothing to re-rank is not a failure.
                "status": "rerank_failed" if top else "ok",
            },
        )
        inserted = cur.fetchone()
        assert inserted is not None
        run_id: int = inserted[0]
        cur.executemany(
            _INSERT_MATCH,
            [
                {"run_id": run_id, "posting_id": r.posting_id, "rank": rank, "score": score}
                for rank, (r, score) in enumerate(ranked, start=1)
            ],
        )

    if not top:
        log.info("match run %s: no candidates passed the filters", run_id)
        return run_id

    result = _rerank(conn, provider, run_id, facts, filters, top)
    if result.outcome != "ok" or result.parsed is None:
        log.warning("match run %s: rerank %s, feed uses stage-1 order", run_id, result.outcome)
        return run_id

    kept, problems = validate_rerank(result.parsed, {r.posting_id: r for r in top})
    for problem in problems:
        log.warning("match run %s: dropped rerank item: %s", run_id, problem)
    if not kept:
        log.warning("match run %s: no valid rerank items, feed uses stage-1 order", run_id)
        return run_id

    # Re-ranked items first in the model's order, then everything it left out (or that
    # was dropped) in stage-1 order.
    reranked_ids = {i.posting_id for i in kept}
    rest = [r for r in top if r.posting_id not in reranked_ids]
    with conn.transaction(), conn.cursor() as cur:
        updates: list[dict[str, Any]] = [
            {
                "run_id": run_id,
                "posting_id": i.posting_id,
                "final_rank": rank,
                "why": i.why.strip(),
                "evidence": i.evidence,
                "skills": i.skills_to_learn,  # deduped and taxonomy-checked
            }
            for rank, i in enumerate(kept, start=1)
        ]
        updates += [
            {
                "run_id": run_id,
                "posting_id": r.posting_id,
                "final_rank": rank,
                "why": None,
                "evidence": [],
                "skills": [],
            }
            for rank, r in enumerate(rest, start=len(kept) + 1)
        ]
        cur.executemany(_UPDATE_MATCH, updates)
        cur.execute(_MARK_RERANKED, (len(kept), run_id))
    return run_id


def feed(conn: psycopg.Connection[Any], match_run_id: int) -> list[dict[str, Any]]:
    """The display rows of one run, best first."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_FEED, (match_run_id,))
        return cur.fetchall()


# --- CLI -----------------------------------------------------------------------------


def _format_pay(ranges: list[dict[str, Any]] | None) -> str:
    if not ranges:
        return "pay not published"

    def one(r: dict[str, Any]) -> str:
        unit = "/hr" if r["pay_type"] == "hourly" else (" OTE" if r["pay_type"] == "ote" else "")
        where = f" ({r['location']})" if r["location"] else ""
        return f"{r['currency']} {r['min']:,}-{r['max']:,}{unit}{where}"

    return "; ".join(one(r) for r in ranges)


def _print_feed(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        track = "/".join(r["track"] or [])
        print(f"{r['final_rank']}. {r['company']} — {r['title']}")
        pay = _format_pay(r["display_salary"])
        print(f"   {track} · {r['seniority']} · {r['remote_policy']} · {pay}")
        if r["why"]:
            print(f"   why: {r['why']}")
        if r["skills_to_learn"]:
            print(f"   skills to learn: {' '.join(r['skills_to_learn'])}")
        print(f"   {r['url']}")


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m jobradar.match")
    p.add_argument("--profile", required=True)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--since-days", type=int, default=30)
    args = p.parse_args(argv)

    init_tracing()
    with connect(autocommit=True) as conn:
        try:
            run_id = run_match(conn, args.profile, top_k=args.top_k, since_days=args.since_days)
        except ProfileNotFoundError as e:
            sys.exit(f"error: {e} (create one with: python -m jobradar.profiles create ...)")
        rows = feed(conn, run_id)
        status = conn.execute("SELECT status FROM match_runs WHERE id = %s", (run_id,)).fetchone()
    print(
        f"match run {run_id} ({status[0] if status else '?'}): {len(rows)} matches", file=sys.stderr
    )
    _print_feed(rows)


if __name__ == "__main__":
    main()
