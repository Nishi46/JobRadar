"""User profiles: a resume turned into taxonomy facts, plus the user's hard filters.

The LLM gives a first draft of `ProfileFacts` from a pasted resume; after that the user
edits it, and the edited version is ground truth (DESIGN.md §4). Matching only ever
reads the facts and filters, never the resume.

Personal data (CLAUDE.md): the resume text is stored only so the user can re-extract
later, and `forget-resume` deletes it. It is never logged, never traced, and never
stored in `llm_runs` (the call is marked `sensitive=True`, which makes the provider
layer, tracing and `record_run` drop input and output text). Log lines here carry
counts, never content: even an invented skill "code" can be a fragment of the resume.
"""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from jobradar.db import connect
from jobradar.facts import Seniority, Track
from jobradar.llm.base import LLMCall, Provider
from jobradar.llm.registry import get_provider
from jobradar.llm_runs import record_run, safe_error
from jobradar.prompts import load_prompt
from jobradar.render import render_taxonomy
from jobradar.taxonomy import SKILL_CODES
from jobradar.tracing import init_tracing, llm_span

log = logging.getLogger(__name__)

PROMPT_NAME = "extract_profile"
# A years value outside this range is a misread (a graduation year, a negative number),
# not a career length; None ("unknown") is more honest than a wrong number.
MAX_PLAUSIBLE_YEARS = 60


class ProfileFacts(BaseModel):
    """What the extraction model returns for a resume, and what the user then edits.

    No validators on purpose: the same model is the structured-output schema, so a bad
    skill code should be *dropped* after parsing (see `clean_extracted`), not make the
    whole output fail validation.
    """

    skills: list[str]
    tracks: list[Track]
    seniority: Seniority
    years_experience: int | None


class MatchFilters(BaseModel):
    """The user's hard filters. An empty list means "no constraint" for that field."""

    tracks: list[Track] = []
    seniorities: list[Seniority] = []
    locations: list[str] = []
    remote_ok: bool = True
    min_pay_usd: int | None = None


class ProfileExtractionError(Exception):
    """The model didn't return usable profile facts (refused, truncated, invalid, API error).
    The message names the outcome only; it never includes resume or output text."""


class ProfileNotFoundError(LookupError):
    pass


# --- SQL -----------------------------------------------------------------------------

# name is UNIQUE: a second create with the same name raises UniqueViolation, which the
# CLI reports instead of silently overwriting someone's edited facts.
_INSERT = """
    INSERT INTO profiles (name, resume_text, facts, filters)
    VALUES (%(name)s, %(resume_text)s, %(facts)s, %(filters)s)
    RETURNING id
"""

_SELECT = "SELECT id, facts, filters FROM profiles WHERE name = %s"

# COALESCE keeps the stored value when the caller passed None for that part, so one
# statement serves "edit facts", "edit filters" and both.
_UPDATE = """
    UPDATE profiles SET
        facts      = COALESCE(%(facts)s, facts),
        filters    = COALESCE(%(filters)s, filters),
        updated_at = now()
    WHERE name = %(name)s
"""

_FORGET_RESUME = "UPDATE profiles SET resume_text = NULL, updated_at = now() WHERE name = %s"

# The extraction run is recorded before the profile exists (it must be recorded even
# when it fails, D-009), so the link is added once the profile row has an id.
_LINK_RUN = "UPDATE llm_runs SET profile_id = %s WHERE id = %s"


# --- extraction ----------------------------------------------------------------------


def clean_extracted(facts: ProfileFacts) -> ProfileFacts:
    """Make model output safe to store: taxonomy-only skills (deduped, order kept),
    `unknown` never mixed with real tracks, and implausible years → None."""
    seen: set[str] = set()
    skills: list[str] = []
    dropped = 0
    for code in facts.skills:
        if code not in SKILL_CODES:
            dropped += 1
        elif code not in seen:
            seen.add(code)
            skills.append(code)
    if dropped:
        log.warning("profile extraction: dropped %d skill code(s) not in the taxonomy", dropped)

    tracks = list(dict.fromkeys(facts.tracks))
    if "unknown" in tracks and len(tracks) > 1:
        tracks.remove("unknown")

    years = facts.years_experience
    if years is not None and not 0 <= years <= MAX_PLAUSIBLE_YEARS:
        years = None

    return ProfileFacts(
        skills=skills, tracks=tracks, seniority=facts.seniority, years_experience=years
    )


def _extract(
    provider: Provider, resume_text: str, conn: psycopg.Connection[Any] | None
) -> tuple[ProfileFacts, int | None]:
    """Run the extraction call. With `conn`, record it in llm_runs (also on failure: a
    failed call still cost money). Returns the cleaned facts and the llm_runs id."""
    prompt = load_prompt(PROMPT_NAME)
    system, user = prompt.render(resume=resume_text, taxonomy=render_taxonomy())
    call = LLMCall(
        purpose="extract_profile",
        prompt_name=prompt.name,
        prompt_version=prompt.version,
        provider=prompt.provider,  # type: ignore[arg-type]  # prompts.py validates it
        model=prompt.model,
        system=system,
        user=user,
        max_tokens=prompt.max_tokens,
        effort=prompt.effort,
        sensitive=True,  # resume text: never traced or stored
    )
    with llm_span(call) as span:
        result = provider.complete(call, ProfileFacts)
        span.set_result(result)

    run_id = record_run(conn, call, result, trace_id=span.trace_id) if conn is not None else None

    if result.outcome != "ok" or result.parsed is None:
        # safe_error is the redacted error llm_runs stores for sensitive calls; the raw
        # output (which may echo the resume) is never surfaced.
        detail = safe_error(call, result) or "no detail"
        raise ProfileExtractionError(
            f"profile extraction failed: outcome={result.outcome} ({detail})"
        )
    return clean_extracted(result.parsed), run_id


def extract_profile(provider: Provider, resume_text: str) -> ProfileFacts:
    """Resume → cleaned ProfileFacts. Raises ProfileExtractionError on any non-ok outcome.
    Doesn't touch the database; `extract_and_create` is the recorded version."""
    facts, _ = _extract(provider, resume_text, conn=None)
    return facts


def extract_and_create(
    conn: psycopg.Connection[Any],
    provider: Provider,
    name: str,
    resume_text: str,
    filters: MatchFilters,
) -> int:
    """Extract, record the llm_runs row, create the profile and link the run to it.
    On extraction failure the run is recorded and no profile is created."""
    facts, run_id = _extract(provider, resume_text, conn)
    with conn.transaction():
        profile_id = create_profile(conn, name, facts, filters, resume_text)
        if run_id is not None:
            conn.execute(_LINK_RUN, (profile_id, run_id))
    return profile_id


# --- table access --------------------------------------------------------------------


def _check_skills(skills: Sequence[str]) -> None:
    """User-edited facts are rejected (not silently cleaned) on unknown codes: a typo the
    user made should be shown to them, not vanish."""
    unknown = sorted(set(skills) - SKILL_CODES)
    if unknown:
        raise ValueError(f"not in the skill taxonomy (docs/SKILLS.md): {', '.join(unknown)}")


def create_profile(
    conn: psycopg.Connection[Any],
    name: str,
    facts: ProfileFacts,
    filters: MatchFilters,
    resume_text: str | None,
) -> int:
    _check_skills(facts.skills)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _INSERT,
            {
                "name": name,
                "resume_text": resume_text,
                "facts": Jsonb(facts.model_dump()),
                "filters": Jsonb(filters.model_dump()),
            },
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always returns the row
        profile_id: int = row[0]
        return profile_id


def get_profile(conn: psycopg.Connection[Any], name: str) -> tuple[int, ProfileFacts, MatchFilters]:
    row = conn.execute(_SELECT, (name,)).fetchone()
    if row is None:
        raise ProfileNotFoundError(f"no profile named {name!r}")
    profile_id, facts, filters = row
    # Validated on the way out too: facts are user-editable jsonb, so the stored value is
    # a boundary just like model output.
    return profile_id, ProfileFacts.model_validate(facts), MatchFilters.model_validate(filters)


def update_profile(
    conn: psycopg.Connection[Any],
    name: str,
    *,
    facts: ProfileFacts | None = None,
    filters: MatchFilters | None = None,
) -> None:
    if facts is not None:
        _check_skills(facts.skills)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            _UPDATE,
            {
                "name": name,
                "facts": Jsonb(facts.model_dump()) if facts is not None else None,
                "filters": Jsonb(filters.model_dump()) if filters is not None else None,
            },
        )
        if cur.rowcount == 0:
            raise ProfileNotFoundError(f"no profile named {name!r}")


def delete_resume_text(conn: psycopg.Connection[Any], name: str) -> None:
    """Forget the resume; the extracted (and edited) facts and the filters stay."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_FORGET_RESUME, (name,))
        if cur.rowcount == 0:
            raise ProfileNotFoundError(f"no profile named {name!r}")


# --- CLI -----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m jobradar.profiles")
    sub = p.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="extract a profile from a resume file")
    create.add_argument("--name", required=True)
    create.add_argument("--resume", required=True, type=Path, help="path to a text/markdown resume")
    create.add_argument("--min-pay", type=int, help="minimum annual pay in USD")
    create.add_argument("--location", action="append", default=[], help="repeatable")
    create.add_argument("--track", action="append", default=[], help="repeatable")
    create.add_argument("--seniority", action="append", default=[], help="repeatable")
    create.add_argument("--no-remote", action="store_true", help="exclude remote-only postings")

    show = sub.add_parser("show", help="print facts and filters (never the resume)")
    show.add_argument("--name", required=True)

    skills = sub.add_parser("set-skills", help="replace the profile's skill codes")
    skills.add_argument("--name", required=True)
    skills.add_argument("codes", nargs="+")

    forget = sub.add_parser("forget-resume", help="delete the stored resume text")
    forget.add_argument("--name", required=True)
    return p


def _print_profile(facts: ProfileFacts, filters: MatchFilters) -> None:
    print(json.dumps({"facts": facts.model_dump(), "filters": filters.model_dump()}, indent=2))


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parser().parse_args(argv)
    try:
        with connect(autocommit=True) as conn:
            if args.cmd == "create":
                # Pydantic checks the track/seniority values the user typed.
                filters = MatchFilters(
                    tracks=args.track,
                    seniorities=args.seniority,
                    locations=args.location,
                    remote_ok=not args.no_remote,
                    min_pay_usd=args.min_pay,
                )
                init_tracing()
                prompt = load_prompt(PROMPT_NAME)
                resume_text = args.resume.read_text()
                extract_and_create(
                    conn, get_provider(prompt.provider), args.name, resume_text, filters
                )
                _, facts, filters = get_profile(conn, args.name)
                print(
                    f"created profile {args.name!r}; review and edit the skills:", file=sys.stderr
                )
                _print_profile(facts, filters)
            elif args.cmd == "show":
                _, facts, filters = get_profile(conn, args.name)
                _print_profile(facts, filters)
            elif args.cmd == "set-skills":
                _, facts, _ = get_profile(conn, args.name)
                codes = list(dict.fromkeys(args.codes))
                update_profile(conn, args.name, facts=facts.model_copy(update={"skills": codes}))
                print(f"{args.name}: skills = {' '.join(codes)}", file=sys.stderr)
            elif args.cmd == "forget-resume":
                delete_resume_text(conn, args.name)
                print(f"{args.name}: resume text deleted", file=sys.stderr)
    except (ProfileNotFoundError, ProfileExtractionError, ValueError) as e:
        # ValueError covers taxonomy rejections and pydantic's ValidationError.
        sys.exit(f"error: {e}")
    except psycopg.errors.UniqueViolation:
        sys.exit(f"error: a profile named {args.name!r} already exists")


if __name__ == "__main__":
    main()
