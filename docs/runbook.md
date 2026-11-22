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
| Extraction failure rate | below _X_% per day | `llm_runs.outcome` | |
| p95 page load | below _N_ ms | | |
| Daily LLM cost | below $_N_ | `llm_runs.cost` | 80% of budget |

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
