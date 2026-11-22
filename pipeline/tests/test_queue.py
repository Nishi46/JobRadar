import random
import uuid
from typing import Any

import psycopg
import pytest

from jobradar.db import connect
from jobradar.queue import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    RETRY_AFTER_CAP_S,
    Job,
    LeaseLost,
    backoff_seconds,
    claim,
    complete,
    enqueue_poll_jobs,
    fail,
    worker_id,
)

_INSERT_BOARD = """
    INSERT INTO boards (ats, board_token, company) VALUES ('greenhouse', %s, 'Test Co')
    RETURNING id
"""


def make_board(conn: psycopg.Connection[Any]) -> int:
    token = f"test-queue-{uuid.uuid4().hex[:12]}"
    row = conn.execute(_INSERT_BOARD, (token,)).fetchone()
    assert row is not None
    return int(row[0])


def job_row(conn: psycopg.Connection[Any], job_id: int) -> dict[str, Any]:
    cur = conn.execute(
        "SELECT status, attempts, locked_by, lease_expires_at, last_error, finished_at,"
        " run_after > now() AS in_future, lease_expires_at > now() AS lease_live"
        " FROM jobs WHERE id = %s",
        (job_id,),
    )
    row = cur.fetchone()
    assert row is not None, f"job {job_id} not found"
    assert cur.description is not None
    return dict(zip([c.name for c in cur.description], row, strict=True))


def expire_lease(conn: psycopg.Connection[Any], job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
        (job_id,),
    )


def active_jobs(conn: psycopg.Connection[Any], board_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM jobs WHERE board_id = %s"
        " AND status IN ('queued', 'retrying', 'running')",
        (board_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture(autouse=True)
def empty_jobs(db: psycopg.Connection[Any]) -> None:
    # raw_fetches.job_id is ON DELETE SET NULL, so jobs can go without touching children.
    # The db fixture rolls this back afterwards.
    db.execute("DELETE FROM jobs")


@pytest.fixture
def board_id(db: psycopg.Connection[Any]) -> int:
    return make_board(db)


# --- enqueue ---


def test_enqueue_twice_inserts_once(db: psycopg.Connection[Any], board_id: int) -> None:
    assert enqueue_poll_jobs(db, [board_id]) == 1
    assert enqueue_poll_jobs(db, [board_id]) == 0
    assert active_jobs(db, board_id) == 1


def test_enqueue_empty_list(db: psycopg.Connection[Any]) -> None:
    assert enqueue_poll_jobs(db, []) == 0


def test_enqueue_duplicate_ids_in_one_call(db: psycopg.Connection[Any], board_id: int) -> None:
    assert enqueue_poll_jobs(db, [board_id, board_id]) == 1


def test_enqueue_after_done_inserts_new_job(db: psycopg.Connection[Any], board_id: int) -> None:
    enqueue_poll_jobs(db, [board_id])
    job = claim(db, "w1")
    assert job is not None
    complete(db, job)

    assert enqueue_poll_jobs(db, [board_id]) == 1
    assert active_jobs(db, board_id) == 1


# --- claim ---


def test_claim_empty_queue_returns_none(db: psycopg.Connection[Any]) -> None:
    assert claim(db, "w1") is None


def test_claim_sets_running_and_lease(db: psycopg.Connection[Any], board_id: int) -> None:
    enqueue_poll_jobs(db, [board_id])

    job = claim(db, "w1")

    assert job is not None
    assert (job.board_id, job.kind, job.attempts, job.locked_by) == (
        board_id,
        "poll_board",
        1,
        "w1",
    )
    row = job_row(db, job.id)
    assert row["status"] == "running"
    assert row["attempts"] == 1
    assert row["locked_by"] == "w1"
    assert row["lease_live"] is True


def test_second_claim_gets_a_different_job(db: psycopg.Connection[Any], board_id: int) -> None:
    other = make_board(db)
    enqueue_poll_jobs(db, [board_id, other])

    first = claim(db, "w1")
    second = claim(db, "w2")
    third = claim(db, "w3")

    assert first is not None and second is not None
    assert first.id != second.id
    assert third is None


def test_future_run_after_is_not_claimable(db: psycopg.Connection[Any], board_id: int) -> None:
    enqueue_poll_jobs(db, [board_id])
    db.execute("UPDATE jobs SET run_after = now() + interval '1 minute'")

    assert claim(db, "w1") is None


def test_expired_lease_is_reclaimed(db: psycopg.Connection[Any], board_id: int) -> None:
    enqueue_poll_jobs(db, [board_id])
    first = claim(db, "w1")
    assert first is not None
    expire_lease(db, first.id)

    second = claim(db, "w2")

    assert second is not None
    assert second.id == first.id
    assert second.attempts == 2
    assert second.locked_by == "w2"
    assert job_row(db, first.id)["lease_live"] is True


def test_live_lease_is_not_reclaimed(db: psycopg.Connection[Any], board_id: int) -> None:
    enqueue_poll_jobs(db, [board_id])
    assert claim(db, "w1") is not None

    assert claim(db, "w2") is None


def test_expired_lease_at_max_attempts_goes_dead(
    db: psycopg.Connection[Any], board_id: int
) -> None:
    crasher = board_id
    healthy = make_board(db)
    enqueue_poll_jobs(db, [crasher])
    db.execute("UPDATE jobs SET max_attempts = 1")
    first = claim(db, "w1")
    assert first is not None
    expire_lease(db, first.id)  # the worker "crashed" on its last attempt
    enqueue_poll_jobs(db, [healthy])

    nxt = claim(db, "w2")

    # The crasher is buried, and the claim moves on to the next candidate.
    assert nxt is not None
    assert nxt.board_id == healthy
    row = job_row(db, first.id)
    assert row["status"] == "dead"
    assert row["attempts"] == 1
    assert row["lease_expires_at"] is None
    assert row["finished_at"] is not None
    assert "lease expired" in row["last_error"]


def test_expired_lease_at_max_attempts_alone_returns_none(
    db: psycopg.Connection[Any], board_id: int
) -> None:
    enqueue_poll_jobs(db, [board_id])
    db.execute("UPDATE jobs SET max_attempts = 1")
    job = claim(db, "w1")
    assert job is not None
    expire_lease(db, job.id)

    assert claim(db, "w2") is None
    assert job_row(db, job.id)["status"] == "dead"
    # Dead is not active, so the board can be polled again.
    assert enqueue_poll_jobs(db, [board_id]) == 1


# --- complete ---


def claimed(db: psycopg.Connection[Any], board_id: int, worker: str = "w1") -> Job:
    enqueue_poll_jobs(db, [board_id])
    job = claim(db, worker)
    assert job is not None
    return job


def test_complete_marks_done(db: psycopg.Connection[Any], board_id: int) -> None:
    job = claimed(db, board_id)
    db.execute("UPDATE jobs SET last_error = 'earlier failure' WHERE id = %s", (job.id,))

    complete(db, job)

    row = job_row(db, job.id)
    assert row["status"] == "done"
    assert row["finished_at"] is not None
    assert row["lease_expires_at"] is None
    assert row["last_error"] is None


def test_complete_after_reclaim_raises_and_leaves_row(
    db: psycopg.Connection[Any], board_id: int
) -> None:
    stale = claimed(db, board_id)
    expire_lease(db, stale.id)
    fresh = claim(db, "w2")
    assert fresh is not None
    before = job_row(db, stale.id)

    with pytest.raises(LeaseLost):
        complete(db, stale)

    assert job_row(db, stale.id) == before
    complete(db, fresh)  # the current holder still can
    assert job_row(db, stale.id)["status"] == "done"


def test_complete_fence_includes_attempts(db: psycopg.Connection[Any], board_id: int) -> None:
    # The same worker re-claiming its own expired job must not be confused with
    # its earlier attempt: locked_by matches, attempts does not.
    stale = claimed(db, board_id, worker="w1")
    expire_lease(db, stale.id)
    again = claim(db, "w1")
    assert again is not None and again.attempts == 2

    with pytest.raises(LeaseLost):
        complete(db, stale)


def test_complete_lease_lost_rolls_back_callers_writes_in_savepoint(
    db: psycopg.Connection[Any], board_id: int
) -> None:
    # The worker calls complete() last inside its own state transaction, so a lost
    # lease must undo that transaction's writes too.
    stale = claimed(db, board_id)
    expire_lease(db, stale.id)
    assert claim(db, "w2") is not None

    with pytest.raises(LeaseLost), db.transaction():
        db.execute("UPDATE boards SET last_job_count = 99 WHERE id = %s", (board_id,))
        complete(db, stale)

    row = db.execute("SELECT last_job_count FROM boards WHERE id = %s", (board_id,)).fetchone()
    assert row == (None,)


# --- fail ---


def test_fail_retryable_schedules_retry(db: psycopg.Connection[Any], board_id: int) -> None:
    job = claimed(db, board_id)

    status = fail(db, job, "HTTP 503", retryable=True, rng=random.Random(1))

    assert status == "retrying"
    row = job_row(db, job.id)
    assert row["status"] == "retrying"
    assert row["in_future"] is True
    assert row["last_error"] == "HTTP 503"
    assert row["lease_expires_at"] is None
    assert row["finished_at"] is None
    assert claim(db, "w2") is None  # not before run_after


def test_fail_retryable_at_max_attempts_goes_dead(
    db: psycopg.Connection[Any], board_id: int
) -> None:
    enqueue_poll_jobs(db, [board_id])
    db.execute("UPDATE jobs SET max_attempts = 1")
    job = claim(db, "w1")
    assert job is not None

    assert fail(db, job, "HTTP 503", retryable=True) == "dead"
    row = job_row(db, job.id)
    assert row["status"] == "dead"
    assert row["finished_at"] is not None


def test_fail_not_retryable_goes_dead(db: psycopg.Connection[Any], board_id: int) -> None:
    job = claimed(db, board_id)

    assert fail(db, job, "HTTP 404: board gone", retryable=False) == "dead"
    row = job_row(db, job.id)
    assert row["status"] == "dead"
    assert row["last_error"] == "HTTP 404: board gone"


def test_fail_truncates_long_errors(db: psycopg.Connection[Any], board_id: int) -> None:
    job = claimed(db, board_id)

    fail(db, job, "x" * 10_000, retryable=False)

    assert len(job_row(db, job.id)["last_error"]) <= 2000


def test_fail_is_fenced(db: psycopg.Connection[Any], board_id: int) -> None:
    stale = claimed(db, board_id)
    expire_lease(db, stale.id)
    assert claim(db, "w2") is not None
    before = job_row(db, stale.id)

    with pytest.raises(LeaseLost):
        fail(db, stale, "boom", retryable=True)

    assert job_row(db, stale.id) == before


def test_retrying_job_is_claimable_once_due(db: psycopg.Connection[Any], board_id: int) -> None:
    job = claimed(db, board_id)
    fail(db, job, "HTTP 503", retryable=True)
    db.execute("UPDATE jobs SET run_after = now() WHERE id = %s", (job.id,))

    again = claim(db, "w2")

    assert again is not None
    assert (again.id, again.attempts) == (job.id, 2)


# --- backoff_seconds (pure) ---


def test_backoff_grows_with_attempts() -> None:
    # Compare the deterministic floors (half of each delay is fixed).
    floors = [backoff_seconds(n, None, random.Random(0)) for n in range(1, 6)]
    assert floors == sorted(floors)
    assert backoff_seconds(1, None, random.Random(0)) >= BACKOFF_BASE_S / 2


def test_backoff_jitter_within_half_to_full_delay() -> None:
    rng = random.Random(42)
    for attempts in range(1, 8):
        delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempts - 1))
        for _ in range(200):
            value = backoff_seconds(attempts, None, rng)
            assert delay / 2 <= value <= delay


def test_backoff_is_capped() -> None:
    rng = random.Random(7)
    assert all(backoff_seconds(50, None, rng) <= BACKOFF_CAP_S for _ in range(100))
    assert backoff_seconds(10_000, None, rng) <= BACKOFF_CAP_S  # no OverflowError


def test_backoff_honors_retry_after() -> None:
    assert backoff_seconds(1, 900.0, random.Random(0)) >= 900.0
    # A short Retry-After doesn't shorten our own backoff.
    assert backoff_seconds(5, 1.0, random.Random(0)) >= BACKOFF_BASE_S * 2**4 / 2


def test_backoff_caps_absurd_retry_after() -> None:
    assert backoff_seconds(1, 365 * 24 * 3600.0, random.Random(0)) == RETRY_AFTER_CAP_S


def test_backoff_ignores_negative_retry_after() -> None:
    value = backoff_seconds(1, -5.0, random.Random(0))
    assert BACKOFF_BASE_S / 2 <= value <= BACKOFF_BASE_S


def test_backoff_deterministic_with_seed() -> None:
    a = [backoff_seconds(3, None, random.Random(123)) for _ in range(3)]
    b = [backoff_seconds(3, None, random.Random(123)) for _ in range(3)]
    assert a == b


# --- worker_id ---


def test_worker_id_is_unique_per_call() -> None:
    assert worker_id() != worker_id()
    assert worker_id().count(":") >= 2


# --- SKIP LOCKED across connections ---


def test_claim_skips_rows_locked_by_another_connection() -> None:
    """Needs committed rows: the db fixture's rows are invisible to other connections."""
    try:
        setup = connect()
    except (RuntimeError, psycopg.OperationalError) as e:
        pytest.skip(f"no database: {e}")
    setup.autocommit = True
    holder = connect()
    claimer = connect()
    # One active job per board (partial unique index), so two boards for two jobs.
    locked_board, free_board = make_board(setup), make_board(setup)
    try:
        # Both sort before anything real in a dev database, so the claim's ORDER BY
        # reaches them first: the locked job, then the free one.
        ids = []
        for board, age in ((locked_board, "1000 years"), (free_board, "999 years")):
            row = setup.execute(
                "INSERT INTO jobs (kind, board_id, run_after)"
                " VALUES ('poll_board', %s, now() - %s::interval) RETURNING id",
                (board, age),
            ).fetchone()
            assert row is not None
            ids.append(row[0])
        locked_id, free_id = ids

        # Connection 1 holds a row lock in an open transaction, like a worker mid-claim.
        holder.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (locked_id,))

        # Without SKIP LOCKED the claim would wait on that lock; fail fast instead of hanging.
        claimer.execute("SET lock_timeout = '2s'")  # also opens claimer's transaction
        job = claim(claimer, "w-concurrent")  # runs as a savepoint inside it

        assert job is not None
        assert job.id == free_id
    finally:
        claimer.rollback()  # undo the claim (and anything it buried in a real dev DB)
        holder.rollback()
        boards = [locked_board, free_board]
        setup.execute("DELETE FROM jobs WHERE board_id = ANY(%s)", (boards,))
        setup.execute("DELETE FROM boards WHERE id = ANY(%s)", (boards,))
        for c in (setup, holder, claimer):
            c.close()
