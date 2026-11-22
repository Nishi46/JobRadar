"""Change detection and idempotency of one poll (ingest.apply_poll) against real Postgres.

These are the M1 gate tests: a rerun creates zero duplicates, and new / edited /
closed / reopened are each detected exactly as DATA_MODEL.md's scenarios describe.
"""

from dataclasses import replace
from typing import Any

import psycopg
import pytest

from jobradar import queue
from jobradar.ats.base import PostingInput
from jobradar.ingest import CLOSE_AFTER_MISSES, PollSummary, apply_poll

Conn = psycopg.Connection[Any]


def posting(external_id: str, **overrides: Any) -> PostingInput:
    fields: dict[str, Any] = {
        "external_id": external_id,
        "url": f"https://example.com/jobs/{external_id}",
        "title": f"Applied AI Engineer {external_id}",
        "description_html": "<p>Build evals.</p><ul><li>Python</li><li>SQL</li></ul>",
        "locations_raw": ["Remote - US"],
        "departments": ["Engineering"],
        "ats_compensation": None,
        "ats_raw": {"id": external_id},
    }
    return PostingInput(**(fields | overrides))


pytestmark = pytest.mark.usefixtures("empty_tables")


@pytest.fixture
def board_id(db: Conn) -> int:
    return make_board(db, "acme")


def make_board(db: Conn, token: str) -> int:
    row = db.execute(
        "INSERT INTO boards (ats, board_token, company) VALUES ('greenhouse', %s, %s) RETURNING id",
        (token, token.title()),
    ).fetchone()
    assert row is not None
    board: int = row[0]
    return board


def poll(db: Conn, board_id: int, postings: list[PostingInput]) -> PollSummary:
    """One full poll the way the worker does it: enqueue, claim, apply."""
    queue.enqueue_poll_jobs(db, [board_id])
    job = queue.claim(db, "test-worker")
    assert job is not None
    return apply_poll(db, board_id, postings, job)


def row(db: Conn, external_id: str) -> dict[str, Any]:
    with db.cursor(row_factory=psycopg.rows.dict_row) as cur:
        result = cur.execute(
            "SELECT * FROM postings WHERE external_id = %s", (external_id,)
        ).fetchone()
    assert result is not None, f"posting {external_id} not found"
    return result


def count_postings(db: Conn) -> int:
    result = db.execute("SELECT count(*) FROM postings").fetchone()
    assert result is not None
    n: int = result[0]
    return n


def test_first_poll_inserts_everything_as_new(db: Conn, board_id: int) -> None:
    summary = poll(db, board_id, [posting("1"), posting("2")])

    assert (summary.new, summary.edited, summary.unchanged, summary.missed) == (2, 0, 0, 0)
    r = row(db, "1")
    assert r["status"] == "open"
    assert r["first_seen"] == r["last_seen"] == r["content_changed_at"]
    assert r["paragraphs"][0] == {"id": 1, "text": "Build evals."}
    assert r["cleaned_text"].startswith("Build evals.")
    assert len(r["content_hash"]) == 64


def test_identical_rerun_creates_zero_rows_and_changes_no_content(db: Conn, board_id: int) -> None:
    # The M1 gate.
    postings = [posting("1"), posting("2")]
    poll(db, board_id, postings)
    before = row(db, "1")

    summary = poll(db, board_id, postings)

    assert count_postings(db) == 2
    assert (summary.new, summary.edited, summary.unchanged) == (0, 0, 2)
    after = row(db, "1")
    assert after["id"] == before["id"]  # same row, so M2 facts keep pointing at it
    assert after["first_seen"] == before["first_seen"]
    assert after["content_changed_at"] == before["content_changed_at"]
    assert after["last_seen"] > before["last_seen"]


def test_changed_description_is_an_edit(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])
    before = row(db, "1")

    summary = poll(db, board_id, [posting("1", description_html="<p>Build agents.</p>")])

    assert summary.edited == 1
    after = row(db, "1")
    assert after["content_hash"] != before["content_hash"]
    assert after["content_changed_at"] > before["content_changed_at"]
    assert after["cleaned_text"] == "Build agents."


@pytest.mark.parametrize(
    "change",
    [
        {"departments": ["Research"]},  # stored, not hashed (D-006)
        {"ats_raw": {"id": "1", "updated_at": "later"}},
        {"description_html": "<p>Build   evals.</p>\n<ul><li>Python</li><li>SQL</li></ul>"},
        {"locations_raw": [" Remote  - US "]},  # whitespace-only difference
    ],
)
def test_changes_the_llm_does_not_read_are_not_edits(
    db: Conn, board_id: int, change: dict[str, Any]
) -> None:
    poll(db, board_id, [posting("1")])

    summary = poll(db, board_id, [replace(posting("1"), **change)])

    assert summary.edited == 0
    assert summary.unchanged == 1


def test_stored_but_unhashed_fields_are_still_updated(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])

    poll(db, board_id, [posting("1", departments=["Research"])])

    assert row(db, "1")["departments"] == ["Research"]


def test_posting_closes_after_three_missed_polls(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1"), posting("2")])

    for miss in range(1, CLOSE_AFTER_MISSES):
        summary = poll(db, board_id, [posting("1")])
        assert (summary.missed, summary.closed) == (1, 0)
        assert row(db, "2")["missed_polls"] == miss
        assert row(db, "2")["status"] == "open"

    summary = poll(db, board_id, [posting("1")])

    assert summary.closed == 1
    closed = row(db, "2")
    assert closed["status"] == "closed"
    assert closed["closed_at"] is not None
    assert row(db, "1")["status"] == "open"


def test_being_seen_again_resets_the_miss_counter(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])
    poll(db, board_id, [])
    poll(db, board_id, [])

    poll(db, board_id, [posting("1")])
    poll(db, board_id, [])  # one miss after the reset: must not close

    assert row(db, "1")["status"] == "open"
    assert row(db, "1")["missed_polls"] == 1


def test_closed_posting_that_reappears_is_reopened(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])
    first_seen = row(db, "1")["first_seen"]
    for _ in range(CLOSE_AFTER_MISSES):
        poll(db, board_id, [])
    assert row(db, "1")["status"] == "closed"

    summary = poll(db, board_id, [posting("1")])

    assert summary.reopened == 1
    assert summary.new == 0
    r = row(db, "1")
    assert r["status"] == "open"
    assert r["missed_polls"] == 0
    assert r["reopened_at"] is not None
    assert r["first_seen"] == first_seen  # never overwritten (D2)
    assert r["closed_at"] is not None  # kept as history of the last closure


def test_closed_postings_do_not_keep_counting_misses(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])
    for _ in range(CLOSE_AFTER_MISSES + 2):
        poll(db, board_id, [])

    assert row(db, "1")["missed_polls"] == CLOSE_AFTER_MISSES


def test_a_poll_only_touches_its_own_board(db: Conn, board_id: int) -> None:
    other = make_board(db, "other")
    poll(db, other, [posting("x")])

    poll(db, board_id, [posting("1")])

    assert row(db, "x")["missed_polls"] == 0


def test_same_external_id_on_two_boards_is_two_postings(db: Conn, board_id: int) -> None:
    other = make_board(db, "other")

    poll(db, board_id, [posting("1")])
    poll(db, other, [posting("1")])

    assert count_postings(db) == 2  # identity includes the board (D-005)


def test_board_observations_are_updated(db: Conn, board_id: int) -> None:
    db.execute("UPDATE boards SET consecutive_failures = 4 WHERE id = %s", (board_id,))

    poll(db, board_id, [posting("1"), posting("2")])

    result = db.execute(
        "SELECT last_polled_at IS NOT NULL, last_job_count, was_empty_last_poll, "
        "consecutive_failures FROM boards WHERE id = %s",
        (board_id,),
    ).fetchone()
    assert result == (True, 2, False, 0)


def test_board_going_empty_is_flagged(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])

    summary = poll(db, board_id, [])

    assert summary.emptied
    assert summary.missed == 1  # an empty board still counts as a poll (D-007)


def test_board_that_was_always_empty_is_not_flagged(db: Conn, board_id: int) -> None:
    assert not poll(db, board_id, []).emptied
    assert not poll(db, board_id, []).emptied


def test_duplicate_external_ids_in_one_response_keep_the_first(db: Conn, board_id: int) -> None:
    summary = poll(db, board_id, [posting("1"), posting("1", title="Second copy")])

    assert summary.seen == 1
    assert row(db, "1")["title"] == "Applied AI Engineer 1"


def test_lost_lease_rolls_back_every_write(db: Conn, board_id: int) -> None:
    queue.enqueue_poll_jobs(db, [board_id])
    job = queue.claim(db, "slow-worker")
    assert job is not None
    # Another worker reclaims the job after our lease expired.
    db.execute(
        "UPDATE jobs SET locked_by = 'fast-worker', attempts = attempts + 1 WHERE id = %s",
        (job.id,),
    )

    with pytest.raises(queue.LeaseLost):
        apply_poll(db, board_id, [posting("1")], job)

    assert count_postings(db) == 0
    result = db.execute("SELECT last_polled_at FROM boards WHERE id = %s", (board_id,)).fetchone()
    assert result == (None,)


def test_completed_poll_marks_its_job_done(db: Conn, board_id: int) -> None:
    poll(db, board_id, [posting("1")])

    result = db.execute("SELECT status, finished_at IS NOT NULL FROM jobs").fetchone()
    assert result == ("done", True)
