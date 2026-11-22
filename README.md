# JobRadar

AI Job Radar watches AI-startup job boards (Greenhouse, Ashby and Lever public APIs), turns each posting into structured fields, ranks postings against a profile, and emails the matches. It's a portfolio project for learning production LLM engineering and evals. See [`docs/PLAN.md`](docs/PLAN.md) for the milestones and [`docs/DESIGN.md`](docs/DESIGN.md) for the design.

**Status:** M1 (ingestion) is done. M2 (LLM extraction) is next.

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env   # local-only dummy credentials
make dev               # start Postgres, apply migrations
make seed              # load the ~165 seed boards into `boards`
make ingest            # fetch every due board into `postings` (about 3.5 min the first time)
make test lint         # 183 tests; ruff + mypy strict
```

Run `make ingest` hourly from cron to build history; see [`docs/runbook.md`](docs/runbook.md).

## How ingestion works

```
companies.csv ─▶ boards ─▶ poller ─▶ jobs (Postgres queue, SKIP LOCKED + lease)
                                        │
                                        ▼
       worker: fetch ─▶ raw_fetches (always, own txn) ─▶ parse (per-ATS adapter)
                                        │
                                        ▼
       one fenced transaction: upsert postings (new / edited / reopened),
       count misses (close after 3), update board, mark job done
```

- **Idempotent:** a unique `(board_id, external_id)` plus `INSERT … ON CONFLICT` means reruns create zero duplicates; a partial unique index allows one active job per board.
- **Change detection:** the content hash covers only what the LLM will read (cleaned title, locations, text). Absence closes a posting only after 3 successful polls.
- **LLM-ready text:** descriptions are cleaned into numbered paragraphs that M2 citations point at.
- **Polite:** one board at a time, a descriptive User-Agent, one layer of backoff, and responses cached by hash. We link to the original postings and never republish them.

Details: [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md), decisions in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Layout

| Path | What |
|---|---|
| `pipeline/` | Python 3.12: ATS clients, queue, worker, poller, migrations, tests |
| `docs/` | Plan, design, data model, decisions, runbook, learning log |
| `infra/` | Docker Compose (local Postgres) |
| `evals/`, `prompts/` | M2–M3 |
| `web/` | M4 |
