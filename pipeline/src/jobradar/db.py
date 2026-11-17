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


def connect() -> psycopg.Connection[Any]:
    return psycopg.connect(database_url())
