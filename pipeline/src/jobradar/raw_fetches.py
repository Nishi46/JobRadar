"""The raw response log (R6): every board fetch, whatever its outcome.

Written in its own committed transaction, *before* parsing and before the state
transaction (D-009). It is evidence, not state: it matters most when something went
wrong (a parser crash, a posting that closed unexpectedly), and those are exactly
the failures that roll the state transaction back.

Doubles as the response cache. When a body is byte-for-byte the same JSON as the
board's last stored body, only its sha256 is recorded and `body` stays NULL, so a
board that rarely changes costs one hash per poll instead of a full copy. To read a
NULL-body row, find the most recent earlier row of the same board with the same
`body_sha256` and a non-NULL body (`body_for`).
"""

import hashlib
import json
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from jobradar.ats.http import FetchResult

# The latest *stored* body for the board: rows with a NULL body don't count, because
# their body lives in an earlier row with the same hash.
_LATEST_STORED_SHA = """
    SELECT body_sha256 FROM raw_fetches
    WHERE board_id = %s AND body IS NOT NULL
    ORDER BY fetched_at DESC, id DESC
    LIMIT 1
"""

_INSERT = """
    INSERT INTO raw_fetches (board_id, job_id, http_status, body, body_sha256, error)
    VALUES (%(board_id)s, %(job_id)s, %(http_status)s, %(body)s, %(body_sha256)s, %(error)s)
    RETURNING id
"""

_BODY_FOR = """
    SELECT body FROM raw_fetches
    WHERE board_id = %s AND body_sha256 = %s AND body IS NOT NULL
    ORDER BY fetched_at DESC, id DESC
    LIMIT 1
"""


def body_sha256(body: Any) -> str:
    """Hash of the parsed JSON in canonical form.

    sort_keys + fixed separators make the hash independent of key order and
    whitespace in the response, which carry no meaning in JSON.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def record_fetch(
    conn: psycopg.Connection[Any], board_id: int, job_id: int | None, result: FetchResult
) -> int:
    """Insert one raw_fetches row and commit it. Returns the row id."""
    sha = body_sha256(result.body) if result.body is not None else None
    with conn.transaction(), conn.cursor() as cur:
        store_body = result.body is not None
        if sha is not None:
            cur.execute(_LATEST_STORED_SHA, (board_id,))
            row = cur.fetchone()
            store_body = row is None or row[0] != sha
        cur.execute(
            _INSERT,
            {
                "board_id": board_id,
                "job_id": job_id,
                "http_status": result.http_status,
                "body": Jsonb(result.body) if store_body else None,
                "body_sha256": sha,
                "error": result.error,
            },
        )
        inserted = cur.fetchone()
        assert inserted is not None
        fetch_id: int = inserted[0]
        return fetch_id


def body_for(conn: psycopg.Connection[Any], board_id: int, sha: str) -> Any | None:
    """The stored JSON body with this hash, for replaying or debugging a fetch."""
    row = conn.execute(_BODY_FOR, (board_id, sha)).fetchone()
    return None if row is None else row[0]
