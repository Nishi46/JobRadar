"""The Postgres job queue (D-001): enqueue, claim, complete, fail.

A claimed job holds a lease; a running job whose lease expired is claimable again, so a
crashed worker's job is rescued by the next claim with no reaper process (D-008).
Every write that ends a run is fenced on (locked_by, attempts): it only lands if this
claim still holds the job, otherwise it raises LeaseLost.

Each function opens `conn.transaction()`: a real transaction that commits on its own
when none is open, or a savepoint when the caller already has one (the worker's state
transaction, or the test fixture's outer transaction).
"""

import os
import random
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import psycopg

LEASE = timedelta(minutes=10)  # D-008: many times longer than a real board run (seconds)
BACKOFF_BASE_S = 60.0
BACKOFF_CAP_S = 3600.0
# A server's Retry-After is honored, but only up to this: a bogus header must not park
# a job for a year.
RETRY_AFTER_CAP_S = 6 * 3600.0
MAX_ERROR_CHARS = 2000

_DEAD_ON_RECLAIM = "lease expired after max attempts; worker probably crashed"

# The partial unique index jobs_one_active_per_board_idx allows one active job per
# (kind, board_id). Naming its predicate in ON CONFLICT makes an existing active job a
# silent no-op, so enqueueing twice is safe (the M1 gate). DO NOTHING also tolerates the
# same id appearing twice in one call.
_ENQUEUE = """
    INSERT INTO jobs (kind, board_id)
    SELECT 'poll_board', board_id FROM unnest(%s::bigint[]) AS t (board_id)
    ON CONFLICT (kind, board_id) WHERE status IN ('queued', 'retrying', 'running')
    DO NOTHING
"""

# A crash is invisible to the worker that crashed, so the claim is where its last
# attempt gets noticed. A running job whose lease expired with no attempts left is
# marked dead here instead of being run again; otherwise a board that kills the worker
# every time would loop forever (attempts are counted at claim time, D-008).
# SKIP LOCKED (explained on _CLAIM) means a row another worker is claiming right now is
# left alone.
_BURY_EXPIRED = """
    UPDATE jobs
    SET status = 'dead', finished_at = now(), lease_expires_at = NULL, last_error = %(error)s
    WHERE id IN (
        SELECT id FROM jobs
        WHERE kind = %(kind)s
          AND status = 'running'
          AND lease_expires_at < now()
          AND attempts >= max_attempts
        FOR UPDATE SKIP LOCKED
    )
"""

# The inner SELECT picks the oldest due job and locks its row. FOR UPDATE alone would
# make a second worker running the same query *wait* for that lock, then find the row
# already taken. SKIP LOCKED makes it skip rows another transaction has locked and take
# the next one instead, so concurrent workers each get a different job without blocking
# each other. The UPDATE runs in the same statement, so the lock is held until the row
# says 'running' and the transaction commits.
# The attempts < max_attempts guard keeps a job _BURY_EXPIRED could not lock (someone
# else held it) from being run past its limit.
_CLAIM = """
    UPDATE jobs
    SET status = 'running',
        attempts = attempts + 1,
        locked_by = %(worker)s,
        locked_at = now(),
        lease_expires_at = now() + %(lease)s
    WHERE id = (
        SELECT id FROM jobs
        WHERE kind = %(kind)s
          AND (
                (status IN ('queued', 'retrying') AND run_after <= now())
             OR (status = 'running' AND lease_expires_at < now()
                 AND attempts < max_attempts)
          )
        ORDER BY run_after, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, kind, board_id, attempts, max_attempts, locked_by
"""

# The fence: (locked_by, attempts) identifies one claim. A reclaim always bumps
# attempts, so even the same worker re-claiming its own expired job can't commit with
# the old Job. The lease time itself is not checked: a late-but-unreclaimed worker may
# still finish, since nobody else has started on the job.
_FENCE = "id = %(id)s AND status = 'running' AND locked_by = %(worker)s AND attempts = %(attempts)s"

_COMPLETE = f"""
    UPDATE jobs
    SET status = 'done', finished_at = now(), lease_expires_at = NULL, last_error = NULL
    WHERE {_FENCE}
"""

# locked_by/locked_at are kept after the run as a record of who ran it last; the CHECK
# only requires lease fields while running.
_RETRY = f"""
    UPDATE jobs
    SET status = 'retrying',
        run_after = now() + %(delay_s)s * interval '1 second',
        lease_expires_at = NULL,
        last_error = %(error)s
    WHERE {_FENCE}
"""

_DIE = f"""
    UPDATE jobs
    SET status = 'dead', finished_at = now(), lease_expires_at = NULL, last_error = %(error)s
    WHERE {_FENCE}
"""

_RNG = random.Random()


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    board_id: int
    attempts: int  # already incremented by the claim
    max_attempts: int
    locked_by: str


class LeaseLost(Exception):
    """A fenced write found the job no longer held by this claim (reclaimed or finished)."""


def worker_id() -> str:
    """A unique worker identity: host and pid for humans, a random suffix for uniqueness
    (pids are reused across container restarts)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def enqueue_poll_jobs(conn: psycopg.Connection[Any], board_ids: Sequence[int]) -> int:
    """Queue one poll_board job per board that has no active job. Returns how many were
    inserted; rerunning inserts nothing."""
    if not board_ids:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_ENQUEUE, (list(board_ids),))
        return cur.rowcount


def claim(conn: psycopg.Connection[Any], worker: str, kind: str = "poll_board") -> Job | None:
    """Claim the oldest due job of `kind` for `worker`, or return None if none is due."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(_BURY_EXPIRED, {"kind": kind, "error": _DEAD_ON_RECLAIM})
        cur.execute(_CLAIM, {"kind": kind, "worker": worker, "lease": LEASE})
        row = cur.fetchone()
    if row is None:
        return None
    job_id, job_kind, board_id, attempts, max_attempts, locked_by = row
    return Job(
        id=job_id,
        kind=job_kind,
        board_id=board_id,
        attempts=attempts,
        max_attempts=max_attempts,
        locked_by=locked_by,
    )


def complete(conn: psycopg.Connection[Any], job: Job) -> None:
    """Mark the job done. Call it as the last statement of the worker's state transaction:
    LeaseLost propagates out and rolls that transaction back, so a worker that lost its
    job can't write stale results over a newer run (D-008)."""
    _fenced_write(conn, _COMPLETE, job, {})


def fail(
    conn: psycopg.Connection[Any],
    job: Job,
    error: str,
    *,
    retryable: bool,
    retry_after_s: float | None = None,
    rng: random.Random | None = None,
) -> str:
    """Record a failed run and return the new status: 'retrying' (with backoff) or
    'dead' (not retryable, or out of attempts). Fenced like complete()."""
    params: dict[str, Any] = {"error": error[:MAX_ERROR_CHARS]}
    if not retryable or job.attempts >= job.max_attempts:
        _fenced_write(conn, _DIE, job, params)
        return "dead"
    params["delay_s"] = backoff_seconds(job.attempts, retry_after_s, rng or _RNG)
    _fenced_write(conn, _RETRY, job, params)
    return "retrying"


def backoff_seconds(attempts: int, retry_after_s: float | None, rng: random.Random) -> float:
    """Seconds to wait before retry number `attempts` + 1.

    Exponential (base * 2**(attempts-1), capped) with "equal jitter": half the delay is
    fixed, half random. Without jitter, boards that failed together (one ATS outage)
    retry together and hit the server in lockstep; the fixed half keeps a floor so a
    retry is never near-immediate. A server's Retry-After is a minimum, capped at
    RETRY_AFTER_CAP_S.
    """
    # Clamp the exponent so a huge attempt count can't overflow the float; 2**32 is far
    # past the cap anyway.
    exponent = min(max(attempts, 1) - 1, 32)
    delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**exponent)
    jittered = delay / 2 + rng.uniform(0, delay / 2)
    if retry_after_s is None:
        return jittered
    return max(jittered, min(max(retry_after_s, 0.0), RETRY_AFTER_CAP_S))


def _fenced_write(
    conn: psycopg.Connection[Any], sql: str, job: Job, params: dict[str, Any]
) -> None:
    fence = {"id": job.id, "worker": job.locked_by, "attempts": job.attempts}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, fence | params)
        if cur.rowcount == 0:
            raise LeaseLost(
                f"job {job.id} is no longer held by {job.locked_by} (attempt {job.attempts})"
            )
