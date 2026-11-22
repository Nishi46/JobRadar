"""Extraction and matching tables: profiles, match_runs, llm_runs, posting_facts, matches;
and extract_posting jobs on the existing queue.

Plain SQL and CHECK constraints instead of ENUMs, for the same reason as 0002: adding a
value to a Postgres ENUM cannot be cleanly rolled back, while a CHECK is just replaced.

Foreign-key choices, and why:
- Anything pointing at `postings` is RESTRICT. Postings are closed, never deleted, and
  a facts row or a match without its posting would be meaningless.
- `llm_runs` points at postings/profiles/match_runs with SET NULL. It is an append-only
  cost and debugging log (D-009): it must outlive whatever it was about, e.g. a profile
  the user deleted.
- `posting_facts.llm_run_id` is SET NULL so a future retention job can trim old
  `llm_runs` rows without touching the facts they produced.
- `match_runs.profile_id` and `matches.match_run_id` CASCADE: a run's matches are part
  of the run, and a run is part of the profile. Deleting a profile (personal data)
  should take its match history with it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The statuses that count as "active" for the one-active-job-per-target indexes. The
# queue's ON CONFLICT clauses must repeat this predicate word for word, or Postgres
# can't match them to the index (see queue.py).
_ACTIVE = "status IN ('queued', 'retrying', 'running')"


def upgrade() -> None:
    # profiles and match_runs come first: llm_runs references both.
    op.execute("""
        CREATE TABLE profiles (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name        text NOT NULL UNIQUE,
            -- Personal data. Nullable so the user can delete it and keep the extracted
            -- facts (profiles.delete_resume_text).
            resume_text text,
            facts       jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'object'),    -- ProfileFacts
            filters     jsonb NOT NULL CHECK (jsonb_typeof(filters) = 'object'),  -- MatchFilters
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE match_runs (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            profile_id     bigint NOT NULL REFERENCES profiles (id) ON DELETE CASCADE,
            created_at     timestamptz NOT NULL DEFAULT now(),
            -- The funnel: considered -> passed filters -> sent to the re-ranker.
            candidates     integer NOT NULL DEFAULT 0 CHECK (candidates >= 0),
            passed_filters integer NOT NULL DEFAULT 0 CHECK (passed_filters >= 0),
            reranked       integer NOT NULL DEFAULT 0 CHECK (reranked >= 0),
            -- rerank_failed: the feed fell back to stage-1 (overlap score) order.
            status         text NOT NULL CHECK (status IN ('ok', 'rerank_failed'))
        )
    """)
    op.execute("CREATE INDEX match_runs_profile_time_idx ON match_runs (profile_id, created_at)")

    op.execute("""
        CREATE TABLE llm_runs (
            id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            created_at         timestamptz NOT NULL DEFAULT now(),
            purpose            text NOT NULL
                               CHECK (purpose IN ('extract_posting', 'extract_profile', 'rerank')),
            provider           text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
            model              text NOT NULL,
            prompt_name        text NOT NULL,
            prompt_version     integer NOT NULL,
            posting_id         bigint REFERENCES postings (id) ON DELETE SET NULL,
            profile_id         bigint REFERENCES profiles (id) ON DELETE SET NULL,
            match_run_id       bigint REFERENCES match_runs (id) ON DELETE SET NULL,
            -- 1 = first call, 2 = the repair retry.
            attempt            integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            input_tokens       integer NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
            output_tokens      integer NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
            cache_read_tokens  integer NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
            cache_write_tokens integer NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
            -- NULL when the model is missing from the price table: unknown, not free.
            cost_usd           numeric(12, 6),
            latency_ms         integer CHECK (latency_ms >= 0),
            outcome            text NOT NULL
                               CHECK (outcome IN ('ok', 'invalid', 'refused', 'truncated',
                                                  'api_error')),
            error              text,
            -- Raw model output. Always NULL for sensitive calls (resumes).
            output             jsonb,
            -- Langfuse/OTel trace id (32 hex), to jump from a row to its trace.
            trace_id           text
        )
    """)
    op.execute("CREATE INDEX llm_runs_created_at_idx ON llm_runs (created_at)")
    op.execute("CREATE INDEX llm_runs_posting_idx ON llm_runs (posting_id)")
    op.execute("CREATE INDEX llm_runs_purpose_time_idx ON llm_runs (purpose, created_at)")

    op.execute("""
        CREATE TABLE posting_facts (
            posting_id       bigint PRIMARY KEY REFERENCES postings (id) ON DELETE RESTRICT,
            status           text NOT NULL CHECK (status IN ('ok', 'failed')),
            -- Validated PostingFacts exactly as the model returned them.
            facts            jsonb,
            -- What users see (decide_pay): may be the ATS ranges, not the model's.
            display_salary   jsonb NOT NULL DEFAULT '[]'
                             CHECK (jsonb_typeof(display_salary) = 'array'),
            pay_source       text NOT NULL DEFAULT 'none'
                             CHECK (pay_source IN ('ats', 'text', 'none')),
            pay_disagreement boolean NOT NULL DEFAULT false,
            -- Validation issues of the last attempt ([] when ok).
            issues           jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(issues) = 'array'),
            -- The postings.content_hash these facts came from; stale when it differs.
            content_hash     text NOT NULL,
            prompt_name      text,
            prompt_version   integer,
            model            text,
            llm_run_id       bigint REFERENCES llm_runs (id) ON DELETE SET NULL,
            extracted_at     timestamptz NOT NULL DEFAULT now(),
            CHECK (status <> 'ok' OR facts IS NOT NULL)
        )
    """)

    op.execute("""
        CREATE TABLE matches (
            match_run_id    bigint NOT NULL REFERENCES match_runs (id) ON DELETE CASCADE,
            posting_id      bigint NOT NULL REFERENCES postings (id) ON DELETE RESTRICT,
            stage1_rank     integer NOT NULL CHECK (stage1_rank >= 1),
            overlap_score   real NOT NULL CHECK (overlap_score >= 0 AND overlap_score <= 1),
            final_rank      integer CHECK (final_rank >= 1),
            why             text,
            -- Paragraph ids of the posting backing `why`.
            why_evidence    integer[] NOT NULL DEFAULT '{}',
            skills_to_learn text[] NOT NULL DEFAULT '{}',
            PRIMARY KEY (match_run_id, posting_id)
        )
    """)
    # The PK covers lookups by run; this one serves "which runs showed this posting".
    op.execute("CREATE INDEX matches_posting_idx ON matches (posting_id)")

    # --- jobs: a second kind on the same queue ---
    # Extraction jobs keep board_id (the posting's board), so board_id stays NOT NULL and
    # every existing query that joins jobs to boards keeps working.
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("""
        ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
        CHECK (kind IN ('poll_board', 'extract_posting'))
    """)
    op.execute("""
        ALTER TABLE jobs ADD COLUMN posting_id bigint REFERENCES postings (id) ON DELETE RESTRICT
    """)
    # A poll job targets a board, an extract job targets a posting: posting_id is set
    # exactly when the kind needs it.
    op.execute("""
        ALTER TABLE jobs ADD CONSTRAINT jobs_posting_id_kind_check
        CHECK ((kind = 'poll_board') = (posting_id IS NULL))
    """)
    # The old index allowed one active job per (kind, board). That would allow only one
    # active extraction per *board*, so it splits into one index per kind, each keyed on
    # that kind's target.
    op.execute("DROP INDEX jobs_one_active_per_board_idx")
    op.execute(f"""
        CREATE UNIQUE INDEX jobs_one_active_poll_idx ON jobs (board_id)
        WHERE kind = 'poll_board' AND {_ACTIVE}
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX jobs_one_active_extract_idx ON jobs (posting_id)
        WHERE kind = 'extract_posting' AND {_ACTIVE}
    """)


def downgrade() -> None:
    # Extraction jobs can't exist under the old kind CHECK, so they go. They are
    # derived work (re-enqueued from stale postings), not data. raw_fetches.job_id is
    # SET NULL, and only poll jobs have raw fetches anyway. boards, postings and
    # raw_fetches are not touched.
    op.execute("DELETE FROM jobs WHERE kind = 'extract_posting'")
    op.execute("DROP INDEX jobs_one_active_extract_idx")
    op.execute("DROP INDEX jobs_one_active_poll_idx")
    op.execute(f"""
        CREATE UNIQUE INDEX jobs_one_active_per_board_idx ON jobs (kind, board_id)
        WHERE {_ACTIVE}
    """)
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_posting_id_kind_check")
    op.execute("ALTER TABLE jobs DROP COLUMN posting_id")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('poll_board'))")

    # Children before the tables they reference.
    op.execute("DROP TABLE matches")
    op.execute("DROP TABLE posting_facts")
    op.execute("DROP TABLE llm_runs")
    op.execute("DROP TABLE match_runs")
    op.execute("DROP TABLE profiles")
