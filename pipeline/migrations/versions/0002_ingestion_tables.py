"""Ingestion tables: boards, postings, jobs, raw_fetches.

Implements docs/DATA_MODEL.md. Written as plain SQL so it reads 1:1 against the
Postgres docs; status columns use CHECK constraints rather than ENUM types because
adding a value to a Postgres ENUM cannot be cleanly rolled back.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE boards (
            id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            -- Configuration: owned by pipeline/seed/companies.csv (D16).
            ats                  text NOT NULL
                                 CHECK (ats IN ('greenhouse', 'ashby', 'lever')),
            board_token          text NOT NULL,
            company              text NOT NULL,
            stage                text,
            in_skills_analysis   boolean NOT NULL DEFAULT false,
            enabled              boolean NOT NULL DEFAULT true,
            -- Observations: owned by the poller; the CSV load step never writes these.
            last_polled_at       timestamptz,
            was_empty_last_poll  boolean,
            last_job_count       integer CHECK (last_job_count >= 0),
            consecutive_failures integer NOT NULL DEFAULT 0
                                 CHECK (consecutive_failures >= 0),
            created_at           timestamptz NOT NULL DEFAULT now(),
            updated_at           timestamptz NOT NULL DEFAULT now(),
            -- D1: a token is only unique within the ATS that issued it.
            UNIQUE (ats, board_token)
        )
    """)

    op.execute("""
        CREATE TABLE postings (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            -- RESTRICT: boards are disabled, never deleted (D15).
            board_id           bigint NOT NULL REFERENCES boards (id) ON DELETE RESTRICT,
            external_id        text NOT NULL,
            url                text NOT NULL,
            title              text NOT NULL,
            locations_raw      text[] NOT NULL DEFAULT '{}',
            departments        text[] NOT NULL DEFAULT '{}',
            description_html   text NOT NULL,
            cleaned_text       text NOT NULL,
            paragraphs         jsonb NOT NULL DEFAULT '[]'
                               CHECK (jsonb_typeof(paragraphs) = 'array'),
            content_hash       text NOT NULL,
            cleaner_version    integer NOT NULL,
            ats_compensation   jsonb,
            ats_raw            jsonb NOT NULL,
            status             text NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open', 'closed')),
            missed_polls       integer NOT NULL DEFAULT 0 CHECK (missed_polls >= 0),
            first_seen         timestamptz NOT NULL DEFAULT now(),
            last_seen          timestamptz NOT NULL DEFAULT now(),
            content_changed_at timestamptz NOT NULL DEFAULT now(),
            closed_at          timestamptz,
            reopened_at        timestamptz,
            -- D1: (board_id) stands for exactly one (ats, board_token).
            UNIQUE (board_id, external_id),
            CHECK (status <> 'closed' OR closed_at IS NOT NULL)
        )
    """)
    op.execute("CREATE INDEX postings_board_status_idx ON postings (board_id, status)")
    op.execute("CREATE INDEX postings_first_seen_idx ON postings (first_seen)")

    op.execute("""
        CREATE TABLE jobs (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind             text NOT NULL CHECK (kind IN ('poll_board')),
            board_id         bigint NOT NULL REFERENCES boards (id) ON DELETE RESTRICT,
            status           text NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'retrying', 'running', 'done', 'dead')),
            attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            max_attempts     integer NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
            run_after        timestamptz NOT NULL DEFAULT now(),
            locked_by        text,
            locked_at        timestamptz,
            lease_expires_at timestamptz,
            last_error       text,
            created_at       timestamptz NOT NULL DEFAULT now(),
            finished_at      timestamptz,
            -- A running job must hold a lease, or nothing can ever reclaim it (D14).
            CHECK (status <> 'running'
                   OR (locked_by IS NOT NULL AND locked_at IS NOT NULL
                       AND lease_expires_at IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX jobs_claim_idx ON jobs (status, run_after)")
    # At most one active job per board and kind: makes enqueueing idempotent
    # (INSERT ... ON CONFLICT DO NOTHING), so running the poller twice is safe.
    op.execute("""
        CREATE UNIQUE INDEX jobs_one_active_per_board_idx ON jobs (kind, board_id)
        WHERE status IN ('queued', 'retrying', 'running')
    """)

    op.execute("""
        CREATE TABLE raw_fetches (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            board_id    bigint NOT NULL REFERENCES boards (id) ON DELETE RESTRICT,
            job_id      bigint REFERENCES jobs (id) ON DELETE SET NULL,
            fetched_at  timestamptz NOT NULL DEFAULT now(),
            http_status integer,
            body        jsonb,
            body_sha256 text,
            error       text
        )
    """)
    op.execute("CREATE INDEX raw_fetches_board_time_idx ON raw_fetches (board_id, fetched_at DESC)")


def downgrade() -> None:
    # Reverse dependency order: children before the tables they reference.
    op.execute("DROP TABLE raw_fetches")
    op.execute("DROP TABLE jobs")
    op.execute("DROP TABLE postings")
    op.execute("DROP TABLE boards")
