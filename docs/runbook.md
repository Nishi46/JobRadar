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

## Common incidents

### Provider outage or 429s / 5xx spike
1. Check the provider status page and the error rate in `llm_runs`.
2. Confirm the circuit breaker opened and traffic moved to the fallback provider.
3. If both providers are failing: postings still show with raw ATS fields ("details pending"). Pause the extraction worker if retries are burning budget.
4. After recovery: re-enqueue failed extractions.

### A job board's API changed or started failing
1. Find the failing board in the dead-letter jobs.
2. Fetch one response manually and compare it with a cached raw response.
3. Fix the normalizer, add a test with the new response shape, re-enqueue.

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
