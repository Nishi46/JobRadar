"""Profiles: resume extraction (fake provider, synthetic resume) and the profiles table.

The resume fixture is invented. These tests also check the personal-data rules: the
call is marked sensitive, nothing it logs contains resume text, and the llm_runs row
stores no output.
"""

import logging
from pathlib import Path
from typing import Any, TypeVar, cast

import psycopg
import pytest
from pydantic import BaseModel

from jobradar.llm.base import LLMCall, LLMResult
from jobradar.profiles import (
    MatchFilters,
    ProfileExtractionError,
    ProfileFacts,
    ProfileNotFoundError,
    create_profile,
    delete_resume_text,
    extract_and_create,
    extract_profile,
    get_profile,
    main,
    update_profile,
)

Conn = psycopg.Connection[Any]
T = TypeVar("T", bound=BaseModel)

RESUME_PATH = Path(__file__).parent / "fixtures" / "profile_synthetic.md"
RESUME = RESUME_PATH.read_text()


class FakeProvider:
    name = "anthropic"

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
        provider="anthropic",
        model="claude-haiku-5-5",
        input_tokens=2000,
        output_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        latency_ms=800,
        cost_usd=0.0003,
    )


EXTRACTED = ProfileFacts(
    skills=["PY", "EVALS", "MADE_UP", "PY", "RELIAB", "BACKEND"],
    tracks=["platform", "product"],
    seniority="mid",
    years_experience=5,
)


# --- extract_profile (no database) ---------------------------------------------------


def test_extract_profile_marks_the_call_sensitive_and_cleans_skills(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeProvider(llm_result(EXTRACTED))
    with caplog.at_level(logging.DEBUG):
        facts = extract_profile(provider, RESUME)

    call = provider.calls[0]
    assert call.sensitive
    assert call.purpose == "extract_profile"
    assert "Examplecorp" in call.user  # the resume does reach the model
    # Unknown codes dropped, duplicates removed, order kept.
    assert facts.skills == ["PY", "EVALS", "RELIAB", "BACKEND"]
    assert facts.tracks == ["platform", "product"]
    # Logged the count of dropped codes, but no resume text and not the codes themselves
    # (a made-up "code" can be a fragment of the resume).
    assert "dropped 1 skill code" in caplog.text
    assert "MADE_UP" not in caplog.text
    assert "Examplecorp" not in caplog.text
    assert "Jordan" not in caplog.text


def test_extract_profile_drops_unknown_when_combined_with_real_tracks() -> None:
    mixed = EXTRACTED.model_copy(update={"tracks": ["unknown", "platform"]})
    facts = extract_profile(FakeProvider(llm_result(mixed)), RESUME)
    assert facts.tracks == ["platform"]


def test_extract_profile_discards_implausible_years() -> None:
    weird = EXTRACTED.model_copy(update={"years_experience": -2})
    assert extract_profile(FakeProvider(llm_result(weird)), RESUME).years_experience is None


@pytest.mark.parametrize("outcome", ["refused", "truncated", "invalid", "api_error"])
def test_extract_profile_raises_on_non_ok_outcome(outcome: str) -> None:
    with pytest.raises(ProfileExtractionError) as err:
        extract_profile(FakeProvider(llm_result(None, outcome=outcome)), RESUME)
    assert outcome in str(err.value)
    assert "Examplecorp" not in str(err.value)


# --- profiles table ------------------------------------------------------------------


FACTS = ProfileFacts(skills=["PY"], tracks=["platform"], seniority="mid", years_experience=4)
FILTERS = MatchFilters(locations=["New York, NY"], min_pay_usd=180_000)


@pytest.mark.usefixtures("empty_tables")
def test_create_and_get_round_trip(db: Conn) -> None:
    pid = create_profile(db, "sam", FACTS, FILTERS, resume_text="synthetic")
    assert get_profile(db, "sam") == (pid, FACTS, FILTERS)


@pytest.mark.usefixtures("empty_tables")
def test_create_rejects_codes_outside_the_taxonomy(db: Conn) -> None:
    bad = FACTS.model_copy(update={"skills": ["PY", "COBOL"]})
    with pytest.raises(ValueError, match="COBOL"):
        create_profile(db, "sam", bad, FILTERS, resume_text=None)


@pytest.mark.usefixtures("empty_tables")
def test_get_missing_profile_raises(db: Conn) -> None:
    with pytest.raises(ProfileNotFoundError):
        get_profile(db, "nobody")


@pytest.mark.usefixtures("empty_tables")
def test_update_changes_only_what_is_given(db: Conn) -> None:
    create_profile(db, "sam", FACTS, FILTERS, resume_text=None)
    edited = FACTS.model_copy(update={"skills": ["PY", "EVALS"]})
    update_profile(db, "sam", facts=edited)
    assert get_profile(db, "sam")[1:] == (edited, FILTERS)

    looser = FILTERS.model_copy(update={"min_pay_usd": None})
    update_profile(db, "sam", filters=looser)
    assert get_profile(db, "sam")[1:] == (edited, looser)


@pytest.mark.usefixtures("empty_tables")
def test_update_validates_skills_and_existence(db: Conn) -> None:
    create_profile(db, "sam", FACTS, FILTERS, resume_text=None)
    with pytest.raises(ValueError, match="NOPE"):
        update_profile(db, "sam", facts=FACTS.model_copy(update={"skills": ["NOPE"]}))
    with pytest.raises(ProfileNotFoundError):
        update_profile(db, "nobody", filters=FILTERS)


@pytest.mark.usefixtures("empty_tables")
def test_delete_resume_text_keeps_the_profile(db: Conn) -> None:
    create_profile(db, "sam", FACTS, FILTERS, resume_text="synthetic resume")
    delete_resume_text(db, "sam")
    row = db.execute("SELECT resume_text FROM profiles WHERE name = 'sam'").fetchone()
    assert row == (None,)
    assert get_profile(db, "sam")[1] == FACTS
    with pytest.raises(ProfileNotFoundError):
        delete_resume_text(db, "nobody")


@pytest.mark.usefixtures("empty_tables")
def test_extract_with_conn_records_a_run_without_output(db: Conn) -> None:
    provider = FakeProvider(llm_result(EXTRACTED))
    pid = extract_and_create(db, provider, "sam", RESUME, FILTERS)

    run = db.execute(
        "SELECT purpose, profile_id, output FROM llm_runs WHERE profile_id = %s", (pid,)
    ).fetchone()
    assert run == ("extract_profile", pid, None)  # sensitive: no stored output
    stored = db.execute("SELECT resume_text FROM profiles WHERE id = %s", (pid,)).fetchone()
    assert stored == (RESUME,)


@pytest.mark.usefixtures("empty_tables")
def test_failed_extraction_is_still_recorded_and_creates_nothing(db: Conn) -> None:
    with pytest.raises(ProfileExtractionError):
        extract_and_create(db, FakeProvider(llm_result(None, "refused")), "sam", RESUME, FILTERS)
    assert db.execute("SELECT count(*) FROM profiles").fetchone() == (0,)
    runs = db.execute("SELECT outcome, profile_id FROM llm_runs").fetchall()
    assert runs == [("refused", None)]


# --- CLI -----------------------------------------------------------------------------


@pytest.mark.usefixtures("empty_tables")
def test_cli_set_skills_and_show(
    db: Conn, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    create_profile(db, "sam", FACTS, FILTERS, resume_text="synthetic resume")
    # The CLI opens its own connection; hand it the test's so the rollback covers it.
    monkeypatch.setattr("jobradar.profiles.connect", lambda **_: _NoClose(db))

    main(["set-skills", "--name", "sam", "PY", "RAG"])
    assert get_profile(db, "sam")[1].skills == ["PY", "RAG"]

    with pytest.raises(SystemExit):
        main(["set-skills", "--name", "sam", "NOPE"])

    main(
        [
            "set-filters", "--name", "sam", "--track", "platform", "--seniority", "mid",
            "--location", "US", "--min-pay", "150000", "--no-remote",
        ]
    )  # fmt: skip
    filters = get_profile(db, "sam")[2]
    assert (filters.tracks, filters.seniorities, filters.locations) == (
        ["platform"],
        ["mid"],
        ["US"],
    )
    assert (filters.min_pay_usd, filters.remote_ok) == (150000, False)
    # set-filters replaces the whole set: flags left out go back to "no constraint".
    main(["set-filters", "--name", "sam"])
    assert get_profile(db, "sam")[2] == MatchFilters()
    with pytest.raises(SystemExit):
        main(["set-filters", "--name", "sam", "--track", "not-a-track"])

    capsys.readouterr()
    main(["show", "--name", "sam"])
    out = capsys.readouterr().out
    assert '"RAG"' in out
    assert "synthetic resume" not in out  # show never prints the resume

    main(["forget-resume", "--name", "sam"])
    assert db.execute("SELECT resume_text FROM profiles").fetchone() == (None,)


class _NoClose:
    """Wraps the test connection so the CLI's `with connect() as conn` doesn't close or
    commit it."""

    def __init__(self, conn: Conn) -> None:
        self.conn = conn

    def __enter__(self) -> Conn:
        return self.conn

    def __exit__(self, *exc: object) -> None:
        return None
