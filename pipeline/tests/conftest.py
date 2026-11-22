from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from jobradar.db import connect


@pytest.fixture
def db() -> Iterator[psycopg.Connection[Any]]:
    """A connection whose work is rolled back after the test.

    Requires a migrated database (`make dev`); skips the test if none is reachable.
    Code under test that opens `conn.transaction()` gets a savepoint inside this
    outer transaction, so nothing it commits outlives the test.
    """
    try:
        conn = connect()
    except (RuntimeError, psycopg.OperationalError) as e:
        pytest.skip(f"no database: {e}")
    try:
        conn.execute("SELECT 1")  # opens the outer transaction (autocommit is off)
        yield conn
    finally:
        conn.rollback()
        conn.close()


# Children before parents: postings, jobs and raw_fetches reference boards with
# ON DELETE RESTRICT, so deleting boards first fails once the dev database holds
# real ingested data.
INGESTION_TABLES = ("raw_fetches", "postings", "jobs", "boards")


@pytest.fixture
def empty_tables(db: psycopg.Connection[Any]) -> None:
    """Start the test from empty ingestion tables; the db fixture rolls this back."""
    for table in INGESTION_TABLES:
        db.execute(f"DELETE FROM {table}")
