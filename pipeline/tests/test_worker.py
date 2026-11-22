"""The worker and poller end to end, with the HTTP layer mocked (no network in tests)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from jobradar import poller, queue
from jobradar.ats.http import make_client
from jobradar.worker import drain, process_job

Conn = psycopg.Connection[Any]
Handler = Callable[[httpx.Request], httpx.Response]
FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_board.json"


pytestmark = pytest.mark.usefixtures("empty_tables")


@pytest.fixture
def board_id(db: Conn) -> int:
    row = db.execute(
        "INSERT INTO boards (ats, board_token, company) "
        "VALUES ('greenhouse', 'acme', 'Acme') RETURNING id"
    ).fetchone()
    assert row is not None
    board: int = row[0]
    return board


@pytest.fixture
def board_body() -> dict[str, Any]:
    body: dict[str, Any] = json.loads(FIXTURE.read_text())
    return body


def client(handler: Handler) -> httpx.Client:
    return make_client(transport=httpx.MockTransport(handler))


def claimed(db: Conn, board_id: int) -> queue.Job:
    queue.enqueue_poll_jobs(db, [board_id])
    job = queue.claim(db, "test-worker")
    assert job is not None
    return job


def scalar(db: Conn, sql: str, *params: Any) -> Any:
    row = db.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def test_happy_path_ingests_the_board(db: Conn, board_id: int, board_body: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=board_body)

    status = process_job(db, client(handler), claimed(db, board_id))

    assert status == "done"
    assert str(seen[0].url) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert scalar(db, "SELECT count(*) FROM postings") == 2
    assert scalar(db, "SELECT count(*) FROM raw_fetches WHERE body IS NOT NULL") == 1


def test_rerun_creates_no_postings_and_stores_no_second_body(
    db: Conn, board_id: int, board_body: Any
) -> None:
    http = client(lambda r: httpx.Response(200, json=board_body))
    process_job(db, http, claimed(db, board_id))

    process_job(db, http, claimed(db, board_id))

    assert scalar(db, "SELECT count(*) FROM postings") == 2
    # Both fetches are logged; the identical second body is stored only as its hash.
    assert scalar(db, "SELECT count(*) FROM raw_fetches") == 2
    assert scalar(db, "SELECT count(*) FROM raw_fetches WHERE body IS NULL") == 1
    assert scalar(db, "SELECT count(DISTINCT body_sha256) FROM raw_fetches") == 1


def test_server_error_retries_and_does_not_count_as_a_poll(
    db: Conn, board_id: int, board_body: Any
) -> None:
    process_job(db, client(lambda r: httpx.Response(200, json=board_body)), claimed(db, board_id))

    status = process_job(db, client(lambda r: httpx.Response(503)), claimed(db, board_id))

    assert status == "retrying"
    # A failed fetch says nothing about which postings exist (D-007).
    assert scalar(db, "SELECT max(missed_polls) FROM postings") == 0
    assert scalar(db, "SELECT consecutive_failures FROM boards") == 1
    assert scalar(db, "SELECT error FROM raw_fetches ORDER BY id DESC LIMIT 1") == "HTTP 503"


def test_not_found_is_dead_immediately(db: Conn, board_id: int) -> None:
    status = process_job(db, client(lambda r: httpx.Response(404)), claimed(db, board_id))

    assert status == "dead"
    assert scalar(db, "SELECT last_error FROM jobs") == "HTTP 404"


def test_malformed_board_is_retried_and_writes_nothing(db: Conn, board_id: int) -> None:
    status = process_job(db, client(lambda r: httpx.Response(200, json={})), claimed(db, board_id))

    assert status == "retrying"
    assert scalar(db, "SELECT last_error FROM jobs").startswith("malformed board")
    assert scalar(db, "SELECT count(*) FROM postings") == 0
    # The body that broke the parser is kept for writing the fix.
    assert scalar(db, "SELECT body FROM raw_fetches") == {}


def test_drain_processes_every_due_job(db: Conn, board_body: Any) -> None:
    for token in ("a", "b", "c"):
        db.execute(
            "INSERT INTO boards (ats, board_token, company) VALUES ('greenhouse', %s, %s)",
            (token, token),
        )
    poller.enqueue_due(db)

    n = drain(db, client(lambda r: httpx.Response(200, json=board_body)), "w", delay_s=0)

    assert n == 3
    assert scalar(db, "SELECT count(*) FROM jobs WHERE status = 'done'") == 3


def test_poller_is_safe_to_run_twice(db: Conn, board_id: int) -> None:
    assert poller.enqueue_due(db) == 1
    assert poller.enqueue_due(db) == 0
    assert scalar(db, "SELECT count(*) FROM jobs") == 1


def test_poller_skips_recently_polled_and_disabled_boards(db: Conn, board_id: int) -> None:
    db.execute("UPDATE boards SET last_polled_at = now() - interval '1 hour'")
    db.execute(
        "INSERT INTO boards (ats, board_token, company, enabled) "
        "VALUES ('greenhouse', 'off', 'Off', false)"
    )

    assert poller.enqueue_due(db) == 0

    db.execute("UPDATE boards SET last_polled_at = now() - interval '7 hours'")
    assert poller.enqueue_due(db) == 1  # only the enabled one
