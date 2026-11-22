"""Database connections for the pipeline (plain psycopg, no ORM)."""

import os
from typing import Any

import psycopg

# DATABASE_URL is shared with Alembic, which needs SQLAlchemy's dialect prefix.
_SQLALCHEMY_PREFIX = "postgresql+psycopg://"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env.")
    return url.replace(_SQLALCHEMY_PREFIX, "postgresql://", 1)


def connect(*, autocommit: bool = False) -> psycopg.Connection[Any]:
    """Open a connection.

    Long-running processes (worker, poller) pass autocommit=True, so each
    `conn.transaction()` block is its own committed transaction and nothing stays
    open between steps. Tests keep the default, so one outer transaction can be
    rolled back at the end and every inner block becomes a savepoint.
    """
    return psycopg.connect(database_url(), autocommit=autocommit)
