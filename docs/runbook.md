# Runbook

> Fill this in during M5. Each section gets real commands and dashboard links once they exist. Keep it short enough to follow at 2 a.m.

## Service map

| Component | Where it runs | How to check it's healthy | Logs |
|---|---|---|---|
| Poller + workers | _Railway / Fly.io_ | | |
| FastAPI | | `GET /healthz` | |
| Next.js app | Vercel | | |
| Postgres | Neon | | |
| Langfuse | | | |
| Email (Resend) | | | |

## SLOs

| SLO | Target | Measured by | Alert when |
|---|---|---|---|
| New posting reaches matched users | within _N_ hours | | |
| Extraction failure rate | below _X_% per day | `llm_runs.outcome`, `posting_facts.status` | |
| p95 page load | below _N_ ms | | |
| Daily LLM cost | below $_N_ | `make cost` / `llm_runs.cost_usd` | 80% of budget |

## Ingestion (M1, runs locally until M5)

**Run it.** `make dev` (Postgres + migrations), `make seed` (CSV → `boards`), then `make ingest` (enqueue due boards, then process every job). One full cycle over 165 boards takes about 3.5 minutes, one board at a time.

**Build history.** Add this to `crontab -e` so a cycle runs every hour; boards are only fetched when they are 6 hours stale (D-013):

```
0 * * * * cd /Users/nishi/Documents/Projects/JobRadar && make ingest >> $HOME/jobradar-ingest.log 2>&1
```

**Health checks** (`docker compose -f infra/docker-compose.yml exec postgres psql -U jobradar`):

```sql
-- Job states: anything other than done/queued deserves a look.
SELECT status, count(*) FROM jobs GROUP BY 1;

-- Dead jobs and why.
SELECT b.ats, b.board_token, j.attempts, j.last_error, j.finished_at
FROM jobs j JOIN boards b ON b.id = j.board_id
WHERE j.status = 'dead' ORDER BY j.finished_at DESC;

-- Boards failing repeatedly or not polled for a day.
SELECT ats, board_token, consecutive_failures, last_polled_at FROM boards
WHERE enabled AND (consecutive_failures > 0 OR last_polled_at < now() - interval '1 day');
```

**Log lines to watch for:**
- `went from N jobs to 0`: a board's postings close after 3 empty polls (D-007). Check the careers page within 12 hours. If the company moved ATS, update the seed CSV.
- `lease lost`: two workers handled one board. It's harmless once, but if it repeats, a run is taking longer than the 10-minute lease.
- `board lists the same posting more than once`: the ATS returned a duplicate. The first copy was kept.

**Replay a fetch.** `raw_fetches` holds every response. A row with `body IS NULL` was identical to an earlier one; get it with `raw_fetches.body_for(conn, board_id, body_sha256)` (D-012).

## Extraction and matching (M2, runs locally until M5)

**Setup.** Put real keys in `.env` (never in `.env.example`):

| Key | Needed for | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | `make extract`, `make profile ARGS="create ..."` | Those commands stop with an error naming the variable |
| `OPENAI_API_KEY` | `make match` (re-rank) | Same |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Traces in Langfuse | Tracing is off; everything still runs, and `llm_runs` still records every call |

Run `make dev` once after pulling M2, so migration 0003 is applied.

**Commands.**

| Command | What it does |
|---|---|
| `make extract LIMIT=25` | Enqueue up to 25 stale postings and extract them. Costs money; drop `LIMIT` for all of them. Ends with a summary line: ok / failed / not finished, cost total and per posting, latency p50 / p95 |
| `make profile ARGS="create --name me --resume ~/resume.md --min-pay 150000 --location 'New York, NY'"` | Extract a profile from a resume. Also `--track`, `--seniority` (repeatable) and `--no-remote` |
| `make profile ARGS="show --name me"` / `set-skills --name me CODE ...` / `forget-resume --name me` | Show facts and filters (never the resume), replace skill codes, delete the stored resume text |
| `make profile ARGS="set-filters --name me --track platform --seniority mid --location US --min-pay 150000"` | Replace the hard filters (same flags as `create`); any flag left out means no constraint |
| `make test` | Unit tests against a separate `jobradar_test` database (created and migrated on demand by `make test-db`), never the dev data |
| `make match PROFILE=me` | Run both matching stages and print the feed (links to the original postings, never descriptions) |
| `make cost` | LLM spend for the last 7 days by purpose, model and prompt version (from `llm_runs`) |

**First live run.** `make extract LIMIT=25`, then read the 25 rows (the failed-extractions query below, plus a few `ok` rows next to their postings) before running the rest. Check `make cost`: cost per posting, and whether `cache_read_tokens` is non-zero (D-014).

**Health checks.**

```sql
-- Outcome counts per purpose, last 24 hours. Anything but ok deserves a look.
SELECT purpose, outcome, count(*), round(sum(cost_usd), 4) AS cost_usd
FROM llm_runs WHERE created_at > now() - interval '1 day'
GROUP BY 1, 2 ORDER BY 1, 2;

-- Failed extractions grouped by issue field: the biggest group is the one to fix.
SELECT i ->> 'field' AS field, count(*)
FROM posting_facts f CROSS JOIN LATERAL jsonb_array_elements(f.issues) AS i
WHERE f.status = 'failed'
GROUP BY 1 ORDER BY 2 DESC;

-- Pay disagreements with the ATS (the ATS value is what users see).
SELECT count(*) FROM posting_facts WHERE pay_disagreement;

-- Stale postings: open, and no facts, edited since, or an older prompt version.
-- Replace 1 with the current extract_posting version.
SELECT count(*) FROM postings p LEFT JOIN posting_facts f ON f.posting_id = p.id
WHERE p.status = 'open'
  AND (f.posting_id IS NULL OR f.content_hash <> p.content_hash
       OR f.prompt_version IS DISTINCT FROM 1);

-- Extract jobs that are stuck or dead.
SELECT status, count(*) FROM jobs WHERE kind = 'extract_posting' GROUP BY 1;
```

The per-issue detail and the pay-disagreement listing are in `DATA_MODEL.md` → Useful queries.

**Re-extract after a prompt change.** Copy `prompts/extract_posting.vN.md` to `vN+1`, bump `version` in its frontmatter (it must match the file name), and make the change there. Model, `max_tokens` and effort live in the frontmatter too, so a model change is also a new version. Every open posting is now stale; run `make extract LIMIT=25` first, compare with the previous version (`make cost`, the failure queries), then run the rest. From M3 on, `make eval` must pass before the new version is committed. A `failed` row on the current hash and version is never retried on its own; only an edit or a new prompt version redoes it.

**Log lines to watch for:**
- `pay disagrees with the ATS`: expected now and then; many in one run means the salary rules in the prompt are off.
- `lease lost, results discarded`: as for ingestion. The `llm_runs` rows of that attempt are still there.
- `dropped rerank item` / `rerank ... feed uses stage-1 order`: the re-rank output failed validation or the call failed; the feed is still shown in overlap-score order.
- `stored facts no longer parse, skipped`: the `PostingFacts` schema changed without a prompt version bump. Bump it and re-extract.

### Refusals or truncation
1. `SELECT outcome, error, posting_id FROM llm_runs WHERE outcome IN ('refused', 'truncated') ORDER BY created_at DESC;`
2. Refusals: open the posting (link, not the stored text) and the trace. A few odd postings are fine; they stay `failed` with no retry (D-017).
3. Truncation: compare `output_tokens` with the prompt's `max_tokens` (8,000). If real postings hit it, raise `max_tokens` in a new prompt version and re-extract.

### Extraction cost spike
1. `make cost`, then the cost-per-posting query in `DATA_MODEL.md` grouped by prompt version.
2. Check `attempt = 2` share (repair retries doubling the cost), `cache_read_tokens` dropping to 0 (the system prompt changed or stopped caching), or a mass re-extraction (a prompt bump or a cleaner bump that changed every hash).
3. Stop a run in progress with Ctrl-C: the current job's lease expires and is picked up next time; nothing is lost.

## Common incidents

### Provider outage or 429s / 5xx spike
1. Check the provider status page and the error rate in `llm_runs`.
2. Confirm the circuit breaker opened and traffic moved to the fallback provider.
3. If both providers are failing: postings still show with raw ATS fields ("details pending"). Pause the extraction worker if retries are burning budget.
4. After recovery: re-enqueue failed extractions.

### A job board's API changed or started failing
1. Find the failing board in the dead jobs (query above). `malformed board: …` names the field the adapter rejected; `HTTP 404` usually means the token changed or the company left that ATS.
2. Pull the body that failed from `raw_fetches` (latest row for that board) and compare it with an older good one.
3. Fix the adapter in `pipeline/src/jobradar/ats/`, and add a made-up fixture with the new shape plus a test. The next `make ingest` re-enqueues the board automatically, because a dead job is no longer active.

### Extraction quality dropped (nothing technically broken)
1. Check the nightly online-eval report and recent "wrong field" feedback.
2. Compare the current prompt and model version with the last good one in `llm_runs`.
3. Roll back the prompt version flag if a recent change caused it. Then add the failing cases to the golden set.

### Cost spike
1. Group `llm_runs` by model and prompt version for the last 24 hours.
2. Look for retry loops, a cache miss rate change, or traffic routed to the expensive model.
3. Lower the daily budget cap if needed while you investigate.

### Bad emails sent to users
1. Pause the digest job.
2. Identify affected users and the prompt version that generated the matches.
3. Write a postmortem (`docs/postmortems/`).

## Rollback

- **Prompt or model change:** flip the version flag back to the last good version; no deploy needed.
- **Code change:** redeploy the previous release on the hosting platform.
- **Migration:** _document the down-migration process here._
