"""The ingestion worker: claim a poll_board job, fetch the board, write the results.

One job, start to finish (DATA_MODEL.md, scenario 1):

1. `queue.claim` takes the next due job and a 10-minute lease (D-008).
2. Fetch the board through the polite HTTP layer (one request, descriptive User-Agent).
3. Log the response to `raw_fetches` and commit, whatever the outcome (D-009).
4. Not OK: fail the job (retry with backoff, or dead) and count a board failure.
   Failed fetches never touch postings, so they never count as a missed poll (D-007).
5. OK: parse with the board's ATS adapter. A malformed board fails the job the same way.
6. `ingest.apply_poll` writes postings + board observations and marks the job done,
   all in one transaction fenced on the lease (D-008).

Concurrency is deliberately one: a worker processes one board at a time and pauses
`POLITE_DELAY_S` between fetches. Hundreds of boards every 6 hours is nothing for
one process, and it keeps us a polite client of free public APIs.
"""

import argparse
import logging
import time
import traceback
from typing import Any

import httpx
import psycopg
from pydantic import ValidationError

from jobradar import ingest, queue
from jobradar.ats.http import FetchOutcome, fetch_board, make_client
from jobradar.ats.registry import adapter_for
from jobradar.db import connect
from jobradar.raw_fetches import record_fetch

log = logging.getLogger(__name__)

POLITE_DELAY_S = 1.0

_BOARD = "SELECT ats, board_token FROM boards WHERE id = %s"
_COUNT_FAILURE = """
    UPDATE boards SET consecutive_failures = consecutive_failures + 1, updated_at = now()
    WHERE id = %s
"""


def process_job(conn: psycopg.Connection[Any], client: httpx.Client, job: queue.Job) -> str:
    """Run one claimed job to a final state for this attempt. Returns the job's new status.

    Never raises for expected failures (HTTP, parsing, lost lease); those become job
    states. A bug in our own code is also caught and recorded as a retryable failure,
    so the attempt limit eventually parks it in `dead` with the traceback in last_error.
    """
    try:
        row = conn.execute(_BOARD, (job.board_id,)).fetchone()
        if row is None:
            return _fail(conn, job, f"board {job.board_id} not found", retryable=False)
        ats, token = row
        adapter = adapter_for(ats)

        result = fetch_board(client, adapter.board_url(token))
        record_fetch(conn, job.board_id, job.id, result)

        if result.outcome is not FetchOutcome.OK:
            return _fail(
                conn,
                job,
                result.error or result.outcome.value,
                retryable=result.outcome is FetchOutcome.RETRYABLE,
                retry_after_s=result.retry_after_s,
            )

        try:
            postings = adapter.parse_board(result.body)
        except ValidationError as e:
            # Retryable: a half-deployed ATS change often fixes itself. If it doesn't,
            # the attempt limit sends the job to dead, and raw_fetches holds the body
            # needed to write a parser fix (runbook: "a job board's API changed").
            return _fail(conn, job, f"malformed board: {e}", retryable=True)

        summary = ingest.apply_poll(conn, job.board_id, postings, job)
        log.info("board %s (%s/%s): %s", job.board_id, ats, token, summary)
        return "done"

    except queue.LeaseLost:
        # Another worker reclaimed this job after our lease expired. Our writes were
        # rolled back; its run is the one that counts. Nothing to record.
        log.warning("job %s: lease lost, results discarded", job.id)
        return "lease_lost"
    except Exception as e:
        log.exception("job %s: unexpected error", job.id)
        detail = "".join(traceback.format_exception_only(e)).strip()
        return _fail(conn, job, f"unexpected error: {detail}", retryable=True)


def _fail(
    conn: psycopg.Connection[Any],
    job: queue.Job,
    error: str,
    *,
    retryable: bool,
    retry_after_s: float | None = None,
) -> str:
    """Fail the job and count a board failure, in one transaction."""
    try:
        with conn.transaction():
            status = queue.fail(conn, job, error, retryable=retryable, retry_after_s=retry_after_s)
            conn.execute(_COUNT_FAILURE, (job.board_id,))
    except queue.LeaseLost:
        log.warning("job %s: lease lost while recording failure: %s", job.id, error)
        return "lease_lost"
    log.warning("job %s: %s (%s)", job.id, status, error)
    return status


def drain(
    conn: psycopg.Connection[Any],
    client: httpx.Client,
    worker: str,
    *,
    max_jobs: int | None = None,
    delay_s: float = POLITE_DELAY_S,
) -> int:
    """Process due jobs until none are left (or `max_jobs`). Returns how many ran.

    Jobs whose backoff hasn't expired yet are not due, so a drain ends even while
    retries are pending; the next scheduled run picks them up.
    """
    processed = 0
    while max_jobs is None or processed < max_jobs:
        job = queue.claim(conn, worker)
        if job is None:
            break
        process_job(conn, client, job)
        processed += 1
        time.sleep(delay_s)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Process due poll_board jobs, then exit.")
    parser.add_argument("--max-jobs", type=int, default=None, help="stop after this many jobs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    worker = queue.worker_id()
    # autocommit: every `conn.transaction()` block is a real, committed transaction, so
    # the claim and the raw fetch are durable before the next step starts.
    with connect(autocommit=True) as conn, make_client() as client:
        n = drain(conn, client, worker, max_jobs=args.max_jobs)
    log.info("worker %s processed %d jobs", worker, n)


if __name__ == "__main__":
    main()
