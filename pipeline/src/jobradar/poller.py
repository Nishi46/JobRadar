"""The poller: enqueue one poll_board job per enabled board that is due.

Safe to run twice, or from two machines at once. The partial unique index
`jobs_one_active_poll_idx` (migration 0003) allows at most one queued/retrying/running job per
board, and `queue.enqueue_poll_jobs` inserts with ON CONFLICT DO NOTHING, so a second
run finds every board already queued and inserts nothing (the M1 gate).

"Due" means never polled, or last polled successfully more than POLL_INTERVAL ago.
Meant to run from cron *every hour*: running more often than the interval is free
(boards that aren't due are skipped), and it means a laptop that slept through one
run catches up at the next instead of waiting six hours.
"""

import logging
from datetime import timedelta
from typing import Any

import psycopg

from jobradar import queue
from jobradar.db import connect

log = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(hours=6)

# A failing board keeps an old last_polled_at, so it stays due and is retried every
# poller run once its previous job has gone dead. consecutive_failures shows how long.
_DUE_BOARDS = """
    SELECT id FROM boards
    WHERE enabled
      AND (last_polled_at IS NULL OR last_polled_at < now() - %s)
    ORDER BY id
"""


def due_board_ids(conn: psycopg.Connection[Any], interval: timedelta = POLL_INTERVAL) -> list[int]:
    return [row[0] for row in conn.execute(_DUE_BOARDS, (interval,)).fetchall()]


def enqueue_due(conn: psycopg.Connection[Any], interval: timedelta = POLL_INTERVAL) -> int:
    """Enqueue a job for every due board. Returns how many new jobs were created."""
    return queue.enqueue_poll_jobs(conn, due_board_ids(conn, interval))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with connect(autocommit=True) as conn:
        n = enqueue_due(conn)
    log.info("enqueued %d poll_board jobs", n)


if __name__ == "__main__":
    main()
