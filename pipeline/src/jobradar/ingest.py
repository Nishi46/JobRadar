"""Write one successful board poll into Postgres: upserts, change detection, closures.

This is the "state transaction" of D-008. Everything a poll changes (the postings,
the board's observations, and marking the job done) happens in ONE transaction, and
the last statement is the fenced `queue.complete`. If this worker lost its lease
while it was working, `complete` raises `LeaseLost`, the whole transaction rolls back,
and the newer run's results are never overwritten with stale ones.

Change detection (DATA_MODEL.md, scenario 3):

- **new**: the upsert inserted the row (`first_seen` is this poll).
- **edited**: the row existed and `content_hash` changed, so `content_changed_at` moved
  to this poll. M2 re-extracts these, so a false "edited" costs money (D-006).
- **reopened**: the row was `closed` and is on the board again (D-007).
- **unchanged**: seen again with the same hash. Only `last_seen` and the stored-but-not
  -hashed fields move.
- **missed**: an `open` posting this poll didn't contain. `missed_polls` goes up by one,
  and the posting closes at `CLOSE_AFTER_MISSES` (D-007).

Why a `polled_at` timestamp instead of `now()`: Postgres freezes `now()` for the whole
transaction, so two polls inside one test transaction would share it, and
"was this row touched by *this* poll?" would be unanswerable. Each poll reads
`clock_timestamp()` once and passes it to every statement, which makes the
classification exact.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from jobradar import queue
from jobradar.ats.base import PostingInput
from jobradar.clean import CLEANER_VERSION, clean_description, content_hash

log = logging.getLogger(__name__)

# D-007: a posting closes after this many consecutive successful polls without it.
CLOSE_AFTER_MISSES = 3

# One statement does insert-or-update and classifies the result.
#
# - ON CONFLICT on the (board_id, external_id) unique constraint makes the database
#   the referee for duplicates (D-005): two workers can't both insert the same posting.
# - In DO UPDATE, `postings.x` is the existing row and `EXCLUDED.x` the incoming one.
#   first_seen is never in the SET list, so it is written exactly once (D2).
# - content_changed_at only moves when the hash differs: that's the "edited" signal.
# - A closed posting that reappears is reopened; closed_at is kept as history.
# - RETURNING compares the timestamps against this poll's polled_at to classify the row.
_UPSERT_POSTING = """
    INSERT INTO postings (
        board_id, external_id, url, title, locations_raw, departments,
        description_html, cleaned_text, paragraphs, content_hash, cleaner_version,
        ats_compensation, ats_raw, status, missed_polls,
        first_seen, last_seen, content_changed_at
    ) VALUES (
        %(board_id)s, %(external_id)s, %(url)s, %(title)s, %(locations_raw)s, %(departments)s,
        %(description_html)s, %(cleaned_text)s, %(paragraphs)s, %(content_hash)s,
        %(cleaner_version)s, %(ats_compensation)s, %(ats_raw)s, 'open', 0,
        %(polled_at)s, %(polled_at)s, %(polled_at)s
    )
    ON CONFLICT (board_id, external_id) DO UPDATE SET
        url                = EXCLUDED.url,
        title              = EXCLUDED.title,
        locations_raw      = EXCLUDED.locations_raw,
        departments        = EXCLUDED.departments,
        description_html   = EXCLUDED.description_html,
        cleaned_text       = EXCLUDED.cleaned_text,
        paragraphs         = EXCLUDED.paragraphs,
        content_hash       = EXCLUDED.content_hash,
        cleaner_version    = EXCLUDED.cleaner_version,
        ats_compensation   = EXCLUDED.ats_compensation,
        ats_raw            = EXCLUDED.ats_raw,
        last_seen          = EXCLUDED.last_seen,
        missed_polls       = 0,
        content_changed_at = CASE
            WHEN postings.content_hash IS DISTINCT FROM EXCLUDED.content_hash
            THEN EXCLUDED.last_seen
            ELSE postings.content_changed_at
        END,
        status             = 'open',
        reopened_at        = CASE
            WHEN postings.status = 'closed' THEN EXCLUDED.last_seen
            ELSE postings.reopened_at
        END
    RETURNING
        first_seen = %(polled_at)s          AS is_new,
        content_changed_at = %(polled_at)s  AS content_changed,
        reopened_at = %(polled_at)s         AS is_reopened
"""

# Every open posting of this board that the upserts above did not touch was missing
# from the response. In an UPDATE, the right-hand side sees the OLD row, so
# `missed_polls + 1` is the new count in every expression.
_MARK_MISSED = """
    UPDATE postings SET
        missed_polls = missed_polls + 1,
        status    = CASE WHEN missed_polls + 1 >= %(close_after)s THEN 'closed' ELSE status END,
        closed_at = CASE WHEN missed_polls + 1 >= %(close_after)s THEN %(polled_at)s
                         ELSE closed_at END
    WHERE board_id = %(board_id)s
      AND status = 'open'
      AND last_seen < %(polled_at)s
    RETURNING status
"""

# FOR UPDATE: serializes two workers finishing the same board (only possible after a
# lease expiry), so the "was non-empty, now empty" comparison reads a stable value.
_LOCK_BOARD = "SELECT last_job_count FROM boards WHERE id = %s FOR UPDATE"

_UPDATE_BOARD_OBSERVATIONS = """
    UPDATE boards SET
        last_polled_at       = %(polled_at)s,
        was_empty_last_poll  = %(job_count)s = 0,
        last_job_count       = %(job_count)s,
        consecutive_failures = 0,
        updated_at           = now()
    WHERE id = %(board_id)s
"""


@dataclass(frozen=True)
class PreparedPosting:
    """A PostingInput plus the cleaner's output: exactly the values one upsert writes."""

    source: PostingInput
    title: str
    cleaned_text: str
    paragraphs: list[dict[str, Any]]
    content_hash: str


@dataclass(frozen=True)
class PollSummary:
    """What one poll changed. Logged by the worker; the counts are what reruns are checked on."""

    board_id: int
    seen: int
    new: int
    edited: int
    reopened: int
    unchanged: int
    missed: int
    closed: int
    emptied: bool  # the board had jobs last poll and has none now (alert, D-007)


def prepare(posting: PostingInput) -> PreparedPosting:
    """Clean and hash one posting. Pure: runs before the transaction opens, so a cleaner
    bug fails the job without holding any locks."""
    # Collapse whitespace in the title so a stray double space is never an "edit".
    title = " ".join(posting.title.split())
    cleaned = clean_description(posting.description_html)
    return PreparedPosting(
        source=posting,
        title=title,
        cleaned_text=cleaned.text,
        paragraphs=[p.to_json() for p in cleaned.paragraphs],
        content_hash=content_hash(title, posting.locations_raw, cleaned.text),
    )


def dedupe(postings: list[PostingInput]) -> list[PostingInput]:
    """Keep the first copy of each external_id.

    An ATS should never list one posting twice, but if one does, the second upsert would
    see the row the first just wrote and misreport it as "unchanged". Keeping the first
    copy is deterministic, and the warning makes the oddity visible.
    """
    counts = Counter(p.external_id for p in postings)
    duplicated = sorted(eid for eid, n in counts.items() if n > 1)
    if duplicated:
        log.warning("board lists the same posting more than once: %s", duplicated)
    seen: set[str] = set()
    unique: list[PostingInput] = []
    for p in postings:
        if p.external_id not in seen:
            seen.add(p.external_id)
            unique.append(p)
    return unique


def apply_poll(
    conn: psycopg.Connection[Any],
    board_id: int,
    postings: list[PostingInput],
    job: queue.Job,
) -> PollSummary:
    """Write one successful poll and mark its job done, atomically (D-008).

    Raises queue.LeaseLost (after rolling everything back) if another worker now owns
    the job. Any other exception also rolls back, and the caller fails the job.
    """
    prepared = [prepare(p) for p in dedupe(postings)]
    counts: Counter[str] = Counter()

    with conn.transaction(), conn.cursor() as cur:
        polled_at = _clock(cur)
        cur.execute(_LOCK_BOARD, (board_id,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"board {board_id} does not exist")
        previous_job_count: int | None = row[0]

        for p in prepared:
            cur.execute(_UPSERT_POSTING, _upsert_params(board_id, p, polled_at))
            result = cur.fetchone()
            assert result is not None  # an upsert always returns its row
            is_new, content_changed, is_reopened = result
            if is_new:
                counts["new"] += 1
            else:
                counts["edited" if content_changed else "unchanged"] += 1
                if is_reopened:
                    counts["reopened"] += 1

        cur.execute(
            _MARK_MISSED,
            {"board_id": board_id, "polled_at": polled_at, "close_after": CLOSE_AFTER_MISSES},
        )
        missed_statuses = [r[0] for r in cur.fetchall()]

        cur.execute(
            _UPDATE_BOARD_OBSERVATIONS,
            {"board_id": board_id, "polled_at": polled_at, "job_count": len(prepared)},
        )

        # Last statement: if the lease is no longer ours this raises, and the
        # `with conn.transaction()` above rolls back every write in this block.
        queue.complete(conn, job)

    summary = PollSummary(
        board_id=board_id,
        seen=len(prepared),
        new=counts["new"],
        edited=counts["edited"],
        reopened=counts["reopened"],
        unchanged=counts["unchanged"],
        missed=len(missed_statuses),
        closed=missed_statuses.count("closed"),
        emptied=len(prepared) == 0 and bool(previous_job_count),
    )
    if summary.emptied:
        # D-007: an empty board still counts as a poll, so its postings start closing.
        # This warning is the 12-hour heads-up before they do.
        log.warning(
            "board %s went from %s jobs to 0; its postings close after %s empty polls",
            board_id,
            previous_job_count,
            CLOSE_AFTER_MISSES,
        )
    return summary


def _clock(cur: psycopg.Cursor[Any]) -> datetime:
    # clock_timestamp() is the real current time, unlike now() (see module docstring).
    cur.execute("SELECT clock_timestamp()")
    row = cur.fetchone()
    assert row is not None
    value: datetime = row[0]
    return value


def _upsert_params(board_id: int, p: PreparedPosting, polled_at: datetime) -> dict[str, Any]:
    src = p.source
    return {
        "board_id": board_id,
        "external_id": src.external_id,
        "url": src.url,
        "title": p.title,
        "locations_raw": src.locations_raw,
        "departments": src.departments,
        "description_html": src.description_html,
        "cleaned_text": p.cleaned_text,
        # Jsonb(...) tells psycopg to send a Python object as jsonb, not as text.
        "paragraphs": Jsonb(p.paragraphs),
        "content_hash": p.content_hash,
        "cleaner_version": CLEANER_VERSION,
        "ats_compensation": (
            Jsonb(src.ats_compensation) if src.ats_compensation is not None else None
        ),
        "ats_raw": Jsonb(src.ats_raw),
        "polled_at": polled_at,
    }
