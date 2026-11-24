"""extract.extract_posting and stale_posting_ids against real Postgres, with a fake Provider.

What these pin down (M2_SPEC section F):
- one repair retry at most, and what goes into it;
- which outcomes become a `failed` posting_facts row and which write nothing at all;
- the ATS pay cross-check and its retry, without leaking the ATS numbers to the model;
- the state transaction: content_hash read at the start, fenced on the job's lease;
- llm_runs rows survive a rolled-back state transaction (D-009).
"""

from typing import Any

import pytest

from extract_fakes import (
    LEVER_PAY_AGREES,
    LEVER_PAY_DISAGREES,
    Conn,
    FakeProvider,
    bad_facts,
    facts_row,
    good_facts,
    llm_result,
    make_board,
    make_posting,
    scalar,
)
from jobradar import queue
from jobradar.extract import ExtractionUnavailable, extract_posting, stale_posting_ids
from jobradar.prompts import load_prompt

pytestmark = pytest.mark.usefixtures("empty_tables")


@pytest.fixture
def posting_id(db: Conn, empty_tables: None) -> int:
    return make_posting(db, make_board(db))


def lever_posting(db: Conn, pay: dict[str, Any]) -> int:
    return make_posting(
        db, make_board(db, ats="lever", token="examplo-lever"), ats_compensation=pay
    )


def run_attempts(db: Conn) -> list[tuple[int, str]]:
    rows = db.execute("SELECT attempt, outcome FROM llm_runs ORDER BY id").fetchall()
    return [(r[0], r[1]) for r in rows]


# --- happy path ---------------------------------------------------------------------


def test_happy_path_writes_ok_facts_from_one_call(db: Conn, posting_id: int) -> None:
    provider = FakeProvider([llm_result("ok", good_facts(), cost_usd=0.002)])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts, outcome.issues) == ("ok", 1, [])
    assert outcome.cost_usd == pytest.approx(0.002)
    row = facts_row(db, posting_id)
    assert row is not None
    assert row["status"] == "ok"
    assert row["facts"] == good_facts().model_dump(mode="json")
    assert row["pay_source"] == "text"
    assert row["pay_disagreement"] is False
    assert row["display_salary"] == [r.model_dump(mode="json") for r in good_facts().salary.ranges]
    assert row["issues"] == []
    assert row["content_hash"] == "hash-1"
    prompt = load_prompt("extract_posting")
    assert (row["prompt_name"], row["prompt_version"]) == (prompt.name, prompt.version)
    assert row["model"] == prompt.model
    assert row["llm_run_id"] == scalar(db, "SELECT id FROM llm_runs")
    assert run_attempts(db) == [(1, "ok")]


def test_the_call_is_built_from_the_prompt_file_and_the_posting(db: Conn, posting_id: int) -> None:
    provider = FakeProvider([llm_result("ok", good_facts())])

    extract_posting(db, provider, posting_id)

    (call,) = provider.calls
    prompt = load_prompt("extract_posting")
    assert call.purpose == "extract_posting"
    assert (call.prompt_name, call.prompt_version) == (prompt.name, prompt.version)
    assert (call.provider, call.model, call.max_tokens) == (
        prompt.provider,
        prompt.model,
        prompt.max_tokens,
    )
    assert call.sensitive is False
    # The rendered posting carries numbered paragraphs, so citations can be checked.
    assert "[4] The base salary range" in call.user
    assert scalar(db, "SELECT posting_id FROM llm_runs") == posting_id


def test_a_fact_may_cite_the_header_as_paragraph_0(db: Conn, posting_id: int) -> None:
    # Locations often appear only in the header (ATS location field), shown as [0].
    facts = good_facts()
    facts = facts.model_copy(
        update={"locations": facts.locations.model_copy(update={"evidence": [0]})}
    )
    provider = FakeProvider([llm_result("ok", facts)])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts, outcome.issues) == ("ok", 1, [])
    assert "[0] " in provider.calls[0].user
    # [0] is derived on every render; it never becomes a stored paragraph.
    stored = scalar(db, "SELECT paragraphs FROM postings WHERE id = %s", posting_id)
    assert [p["id"] for p in stored] == [1, 2, 3, 4, 5, 6]


# --- repair retry -------------------------------------------------------------------


def test_repair_retry_fixes_a_validation_issue(db: Conn, posting_id: int) -> None:
    provider = FakeProvider([llm_result("ok", bad_facts()), llm_result("ok", good_facts())])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts, outcome.issues) == ("ok", 2, [])
    assert outcome.cost_usd == pytest.approx(0.002)  # both attempts are paid for
    # The retry shows the model its previous answer and what was wrong with it.
    first, repair = provider.calls
    assert repair.user.startswith(first.user)
    assert "NOT_A_SKILL" in repair.user
    assert "required_skills[0].code" in repair.user
    assert repair.system == first.system  # unchanged system prompt keeps the cache warm
    assert run_attempts(db) == [(1, "ok"), (2, "ok")]
    row = facts_row(db, posting_id)
    assert row is not None
    assert row["status"] == "ok"
    # The row points at the run whose output it stores: the repair.
    assert row["llm_run_id"] == scalar(db, "SELECT id FROM llm_runs WHERE attempt = 2")


def test_still_invalid_after_the_retry_is_failed_with_issues(db: Conn, posting_id: int) -> None:
    provider = FakeProvider([llm_result("ok", bad_facts()), llm_result("ok", bad_facts())])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("failed", 2)
    assert [i.field for i in outcome.issues] == ["required_skills[0].code"]
    row = facts_row(db, posting_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["facts"] is None
    assert row["issues"][0]["field"] == "required_skills[0].code"
    assert row["issues"][0]["message"]
    assert (row["display_salary"], row["pay_source"]) == ([], "none")
    assert len(provider.calls) == 2  # one repair retry, never more


def test_schema_invalid_output_gets_the_repair_retry(db: Conn, posting_id: int) -> None:
    provider = FakeProvider(
        [
            llm_result("invalid", raw_text='{"track": "oops"}', error="track: not a list"),
            llm_result("ok", good_facts()),
        ]
    )

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("ok", 2)
    repair = provider.calls[1]
    assert '{"track": "oops"}' in repair.user
    assert "track: not a list" in repair.user


def test_schema_invalid_twice_is_failed(db: Conn, posting_id: int) -> None:
    invalid = llm_result("invalid", raw_text="{}", error="missing fields")
    provider = FakeProvider([invalid, invalid])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("failed", 2)
    assert outcome.issues[0].field == "schema"
    assert "missing fields" in outcome.issues[0].message


@pytest.mark.parametrize("bad_outcome", ["refused", "truncated"])
def test_refused_or_truncated_fails_without_a_retry(
    db: Conn, posting_id: int, bad_outcome: Any
) -> None:
    # A retry can't help: the same input refuses again, or overflows max_tokens again.
    provider = FakeProvider([llm_result(bad_outcome, error=f"stop_reason {bad_outcome}")])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("failed", 1)
    assert outcome.issues[0].field == bad_outcome
    row = facts_row(db, posting_id)
    assert row is not None
    assert (row["status"], row["facts"]) == ("failed", None)
    assert run_attempts(db) == [(1, bad_outcome)]


# --- transient API errors -----------------------------------------------------------


def test_retryable_api_error_writes_no_facts(db: Conn, posting_id: int) -> None:
    provider = FakeProvider(
        [llm_result("api_error", error="HTTP 529 overloaded", retryable=True, cost_usd=None)]
    )

    with pytest.raises(ExtractionUnavailable) as info:
        extract_posting(db, provider, posting_id)

    assert info.value.retryable is True
    assert "529" in str(info.value)
    # An outage says nothing about the posting: no failed row, so it stays stale.
    assert facts_row(db, posting_id) is None
    assert run_attempts(db) == [(1, "api_error")]  # but the evidence is logged (D-009)


def test_api_error_during_the_repair_also_writes_nothing(db: Conn, posting_id: int) -> None:
    provider = FakeProvider(
        [
            llm_result("ok", bad_facts()),
            llm_result("api_error", error="connection reset", retryable=True),
        ]
    )

    with pytest.raises(ExtractionUnavailable) as info:
        extract_posting(db, provider, posting_id)

    assert info.value.cost_usd == pytest.approx(0.002)
    assert facts_row(db, posting_id) is None
    assert run_attempts(db) == [(1, "ok"), (2, "api_error")]


def test_non_retryable_api_error_is_reported_as_such(db: Conn, posting_id: int) -> None:
    provider = FakeProvider([llm_result("api_error", error="HTTP 401", retryable=False)])

    with pytest.raises(ExtractionUnavailable) as info:
        extract_posting(db, provider, posting_id)

    assert info.value.retryable is False
    assert facts_row(db, posting_id) is None


# --- ATS pay cross-check ------------------------------------------------------------


def test_ats_disagreement_retries_then_displays_ats_pay(db: Conn) -> None:
    posting_id = lever_posting(db, LEVER_PAY_DISAGREES)
    provider = FakeProvider([llm_result("ok", good_facts()), llm_result("ok", good_facts())])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("ok", 2)
    row = facts_row(db, posting_id)
    assert row is not None
    assert row["pay_source"] == "ats"
    assert row["pay_disagreement"] is True
    assert [(r["min"], r["max"]) for r in row["display_salary"]] == [(250000, 300000)]
    # The model's own answer is stored untouched, so ATS pay stays an honest M3 label.
    assert row["facts"]["salary"]["ranges"][0]["min"] == 180000
    # ...which is also why the retry never shows the model the ATS numbers.
    repair = provider.calls[1]
    assert "250" not in repair.user.removeprefix(provider.calls[0].user)
    assert "salary" in repair.user.removeprefix(provider.calls[0].user)


def test_ats_agreement_needs_no_retry(db: Conn) -> None:
    posting_id = lever_posting(db, LEVER_PAY_AGREES)
    provider = FakeProvider([llm_result("ok", good_facts())])

    outcome = extract_posting(db, provider, posting_id)

    assert outcome.attempts == 1
    row = facts_row(db, posting_id)
    assert row is not None
    assert (row["pay_source"], row["pay_disagreement"]) == ("ats", False)


def test_disagreement_after_a_repair_gets_no_third_call(db: Conn) -> None:
    # One retry total keeps the worst-case cost at two calls per posting.
    posting_id = lever_posting(db, LEVER_PAY_DISAGREES)
    provider = FakeProvider([llm_result("ok", bad_facts()), llm_result("ok", good_facts())])

    outcome = extract_posting(db, provider, posting_id)

    assert (outcome.status, outcome.attempts) == ("ok", 2)
    row = facts_row(db, posting_id)
    assert row is not None
    assert (row["pay_source"], row["pay_disagreement"]) == ("ats", True)


# --- the state transaction ----------------------------------------------------------


def test_hash_is_read_at_the_start_so_a_mid_flight_edit_leaves_facts_stale(
    db: Conn, posting_id: int
) -> None:
    def edit_posting(_: int) -> None:
        db.execute("UPDATE postings SET content_hash = 'hash-2' WHERE id = %s", (posting_id,))

    provider = FakeProvider([llm_result("ok", good_facts())], on_call=edit_posting)
    extract_posting(db, provider, posting_id)

    row = facts_row(db, posting_id)
    assert row is not None
    assert row["content_hash"] == "hash-1"
    version = load_prompt("extract_posting").version
    assert stale_posting_ids(db, prompt_version=version) == [posting_id]


def test_rerun_overwrites_the_previous_facts_row(db: Conn, posting_id: int) -> None:
    extract_posting(db, FakeProvider([llm_result("refused", error="no")]), posting_id)

    extract_posting(db, FakeProvider([llm_result("ok", good_facts())]), posting_id)

    assert scalar(db, "SELECT count(*) FROM posting_facts") == 1
    row = facts_row(db, posting_id)
    assert row is not None
    assert (row["status"], row["issues"]) == ("ok", [])


def claimed_job(db: Conn, posting_id: int) -> queue.Job:
    queue.enqueue_extract_jobs(db, [posting_id])
    job = queue.claim(db, "test-worker", kind="extract_posting")
    assert job is not None
    assert job.posting_id == posting_id
    return job


def test_with_a_job_the_facts_and_done_commit_together(db: Conn, posting_id: int) -> None:
    job = claimed_job(db, posting_id)

    extract_posting(db, FakeProvider([llm_result("ok", good_facts())]), posting_id, job)

    assert scalar(db, "SELECT status FROM jobs WHERE id = %s", job.id) == "done"
    assert facts_row(db, posting_id) is not None


def test_lease_lost_rolls_back_the_facts_but_keeps_llm_runs(db: Conn, posting_id: int) -> None:
    job = claimed_job(db, posting_id)
    # Another worker reclaimed the job (a reclaim always bumps attempts).
    db.execute(
        "UPDATE jobs SET locked_by = 'other', attempts = attempts + 1 WHERE id = %s", (job.id,)
    )

    with pytest.raises(queue.LeaseLost):
        extract_posting(db, FakeProvider([llm_result("ok", good_facts())]), posting_id, job)

    assert facts_row(db, posting_id) is None
    assert run_attempts(db) == [(1, "ok")]


def test_unknown_posting_raises(db: Conn) -> None:
    with pytest.raises(ValueError, match="posting"):
        extract_posting(db, FakeProvider([]), 999_999_999)


# --- staleness ----------------------------------------------------------------------


def write_facts(db: Conn, posting_id: int, *, content_hash: str, prompt_version: int) -> None:
    db.execute(
        "INSERT INTO posting_facts (posting_id, status, issues, content_hash, prompt_version) "
        "VALUES (%s, 'failed', '[]', %s, %s)",
        (posting_id, content_hash, prompt_version),
    )


def test_stale_posting_ids(db: Conn) -> None:
    board = make_board(db)
    no_facts = make_posting(db, board, external_id="a", first_seen_offset="1 hour")
    fresh = make_posting(db, board, external_id="b", first_seen_offset="2 hours")
    edited = make_posting(db, board, external_id="c", first_seen_offset="3 hours")
    old_prompt = make_posting(db, board, external_id="d", first_seen_offset="4 hours")
    make_posting(db, board, external_id="e", status="closed")
    write_facts(db, fresh, content_hash="hash-1", prompt_version=1)
    write_facts(db, edited, content_hash="older-hash", prompt_version=1)
    write_facts(db, old_prompt, content_hash="hash-1", prompt_version=0)

    # Oldest first; the fresh row (even though it failed) and the closed posting are not stale.
    assert stale_posting_ids(db, prompt_version=1) == [old_prompt, edited, no_facts]
    assert stale_posting_ids(db, prompt_version=1, limit=2) == [old_prompt, edited]
    # A prompt bump makes everything with facts stale again.
    assert set(stale_posting_ids(db, prompt_version=2)) == {no_facts, fresh, edited, old_prompt}
