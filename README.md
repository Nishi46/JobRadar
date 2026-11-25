# JobRadar

AI Job Radar watches AI-startup job boards (Greenhouse, Ashby and Lever public APIs), turns each posting into structured fields, ranks postings against a profile, and emails the matches. It's a portfolio project for learning production LLM engineering and evals. See [`docs/PLAN.md`](docs/PLAN.md) for the milestones and [`docs/DESIGN.md`](docs/DESIGN.md) for the design.

**Status:** M1 (ingestion) is done. M2 (LLM extraction and matching) is code complete and tested with faked providers; the first live run is waiting on API keys. M3 (evals) is next.

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env   # local-only dummy credentials
make dev               # start Postgres, apply migrations
make seed              # load the ~165 seed boards into `boards`
make ingest            # fetch every due board into `postings` (about 3.5 min the first time)
make test lint         # 556 tests on a separate test DB; ruff + mypy strict
```

M2 needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` in `.env` (Langfuse keys are optional; tracing is off without them). These cost money:

```sh
make extract LIMIT=25                                         # extract 25 stale postings; drop LIMIT for all
make profile ARGS="create --name me --resume ~/resume.md --min-pay 150000"
make match PROFILE=me                                         # filter, score, re-rank, print the feed
make cost                                                     # LLM spend for the last 7 days
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

## How extraction and matching work

```
stale postings ─▶ jobs (kind extract_posting, same queue) ─▶ render prompt ([0] header + [1..n] paragraphs)
                                        │
                                        ▼
       Haiku call ─▶ llm_runs (always, own txn) + trace ─▶ validate in code
                                        │   invalid or pay disagrees with ATS: ONE retry
                                        ▼
       one fenced transaction: upsert posting_facts (ok / failed + issues), mark job done

profile (resume ─▶ taxonomy skills, user-editable) + filters
       ─▶ code filters (unknown passes) ─▶ skill-overlap score ─▶ top 20
       ─▶ GPT re-rank (why + skills to learn, citations checked) ─▶ matches
```

- **Validated in code:** every field cites paragraph ids that must exist; every salary number must appear in the cited paragraph; skills must be in the 34-code taxonomy. Nothing the model returns is stored or shown unchecked.
- **Bounded cost:** at most two calls per posting, and every call's tokens, cost and latency go to `llm_runs` from the first run.
- **ATS pay stays a label:** when the job board publishes pay, users see it, and the model is never shown it, so it stays an independent check for M3.
- **Model tiering:** a small model for thousands of postings, a stronger one for 20 candidates per user. The model lives in the prompt file, so changing it is a version bump.
- **Personal data:** resume and re-rank calls keep counts and cost only, no text in `llm_runs` or traces.

Details: [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md), decisions in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Layout

| Path | What |
|---|---|
| `pipeline/` | Python 3.12: ATS clients, queue, workers, extraction, matching, LLM providers, migrations, tests |
| `docs/` | Plan, design, data model, decisions, runbook, learning log |
| `infra/` | Docker Compose (local Postgres) |
| `prompts/` | Versioned prompt files (`extract_posting.v1.md`, ...) |
| `evals/` | M3 |
| `web/` | M4 |
