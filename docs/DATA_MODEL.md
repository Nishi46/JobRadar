# Data model (M1: ingestion, M2: extraction and matching)

Sketch of the tables. M1 is below; M2 starts at [M2: extraction and matching](#m2-extraction-and-matching).
`D#` refers to the decisions made in the 2026-11-17 design walkthrough. The main ones are written up in `DECISIONS.md`: D1 → D-005, D4 + D5 → D-006, D6 + D7 + D9 + D10 → D-007, D14 → D-008.
Implemented by `pipeline/migrations/versions/0002_ingestion_tables.py`.

## boards

One row per job board. Seeded from `pipeline/seed/companies.csv` (D16).

| Column | Type | Owner | Notes |
|---|---|---|---|
| `id` | bigint PK | — | Surrogate key, so postings and jobs reference one column |
| `ats` | text, check in (`greenhouse`, `ashby`, `lever`) | CSV | Part of identity (D1) |
| `board_token` | text | CSV | Stored exactly as the API URL needs it; never lowercased (Ashby is case-sensitive) |
| `company` | text | CSV | |
| `stage` | text | CSV | |
| `in_skills_analysis` | bool | CSV | |
| `enabled` | bool | CSV | `= verified` from the CSV; a row removed from the CSV becomes `false`, never deleted (D15) |
| `last_polled_at` | timestamptz null | system | Last successful poll |
| `was_empty_last_poll` | bool null | system | Drives the non-empty → empty alert (D9) |
| `last_job_count` | int null | system | Set by the first real poll, not copied from `jobs_at_check` |
| `consecutive_failures` | int, default 0 | system | Reset on a successful poll |
| `created_at`, `updated_at` | timestamptz | — | |

- **Unique:** `(ats, board_token)`
- **Load step:** upserts on `(ats, board_token)` and only ever writes CSV-owned columns (D16). Rows in the table but missing from the CSV get `enabled = false`.
- CSV rows with an out-of-scope ATS (`gem`, `rippling`, `custom`) are not loaded at all, so the check constraint on `ats` holds.

## postings

One row per posting, holding **current text only** (D5).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | Stable ID that facts, feedback and notifications will reference |
| `board_id` | bigint FK → boards, on delete restrict | |
| `external_id` | text | The ATS's posting ID |
| `url` | text | Link to the original posting (R8) |
| `title` | text | Hashed (D4) |
| `locations_raw` | text[] | As the ATS gives them; hashed (D4). Normalized locations come in M2 |
| `departments` | text[] | Stored, **not** hashed (D4) |
| `description_html` | text | Raw HTML as received (HTML-unescaped for Greenhouse) |
| `cleaned_text` | text | Output of the cleaner; hashed (D4) |
| `paragraphs` | jsonb | `[{"id": 1, "text": "..."}, ...]`; position IDs (D12), written in the same `UPDATE` as `cleaned_text` (D11) |
| `content_hash` | text | Hash of cleaned title + locations + cleaned text (D4) |
| `cleaner_version` | int | Bumped when the cleaner changes, so a mass hash change is explainable (D4) |
| `ats_compensation` | jsonb null | ATS structured pay, stored untouched: free salary ground truth for M3 (R7) |
| `ats_raw` | jsonb | the full per-posting item from the API. Keeps every "stored but not hashed" field (compliance flags, deadline, `updated_at`) without a column for each |
| `status` | text, check in (`open`, `closed`) | Current state only (D10) |
| `missed_polls` | int, default 0 | Incremented when absent from a successful poll; reset when seen; close at 3 (D6) |
| `first_seen` | timestamptz | Set on insert, never overwritten (D2) |
| `last_seen` | timestamptz | Updated on every poll where it appears (D2) |
| `content_changed_at` | timestamptz | Set on insert, updated only when `content_hash` changes (D2) |
| `closed_at` | timestamptz null | set when `status` becomes `closed` |
| `reopened_at` | timestamptz null | Set when a closed posting reappears (D10) |

- **Unique:** `(board_id, external_id)`. This is D1's `(ats, board_token, external_id)`, because `board_id` stands for exactly one `(ats, board_token)`.
- **Indexes:** `(board_id, status)` for the close step; `(first_seen)` for "new this week".

## jobs

The Postgres queue (D-001). M1 has one kind of job: poll one board.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `kind` | text | `poll_board`; M2 adds `extract_posting` (see M2 below) |
| `board_id` | bigint FK → boards | |
| `status` | text, check in (`queued`, `retrying`, `running`, `done`, `dead`) | D13 |
| `attempts` | int, default 0 | Incremented at **claim** time, so crashes count (D14) |
| `max_attempts` | int, default 5 | Placeholder; tuned in M5 |
| `run_after` | timestamptz | Claimable once `run_after <= now()`; backoff with jitter sets it (D13) |
| `locked_by` | text null | Worker identity, used by the fenced commit (D14) |
| `locked_at` | timestamptz null | |
| `lease_expires_at` | timestamptz null | `locked_at` + ~10 min; an expired `running` job is claimable again (D14) |
| `last_error` | text null | Why it is retrying or dead |
| `created_at`, `finished_at` | timestamptz | |

- **Claimable:** (`status in (queued, retrying)` and `run_after <= now()`) or (`status = running` and `lease_expires_at < now()` and `attempts < max_attempts`). An expired job with no attempts left is marked `dead` by the claim itself (D-010).
- **Fence token:** `(locked_by, attempts)`. `complete` and `fail` only write when both still match (D-008, D-010).
- **Index:** `(status, run_after)` for the claim query.
- **at most one active job per board**, as a partial unique index on `(kind, board_id)` where `status in (queued, retrying, running)`. This makes the poller's enqueue idempotent ("safe to run twice", PLAN M1) with `ON CONFLICT DO NOTHING`, the same pattern as D1. M2 splits this index in two (see `jobs` changes below).

## raw_fetches (R6)

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `board_id` | bigint FK → boards | |
| `job_id` | bigint FK → jobs | Which job made the fetch |
| `fetched_at` | timestamptz | |
| `http_status` | int null | Null when there was no response (timeout) |
| `body` | jsonb null | The full response; null on failure |
| `body_sha256` | text null | Skip storing `body` when identical to the board's previous fetch |
| `error` | text null | |

- **Dedupe as cache (D-012):** a body identical (canonical-JSON sha256) to the board's last *stored* body is recorded as its hash only, with `body = NULL`. `raw_fetches.body_for(board_id, sha)` finds the stored copy.
- **Measured 2026-11-21** (165 boards, 4,668 postings): one full set of distinct bodies is about 28 MB, and the largest single board is 1.9 MB. An unchanged rerun adds about 0 MB, because every body was stored as its hash only. `postings` itself is about 92 MB, since it holds `description_html`, `cleaned_text`, `paragraphs` and `ats_raw`.
- Retention (not enforced yet): keep 14 days, plus the latest fetch per board. At about 28 MB per *changed* full set, this fits Neon's 0.5 GB free tier only if boards change slowly; re-measure after a week of hourly runs.
- Doubles as the debugging record when a posting closes unexpectedly: "was it really missing from the response?"

## How a poll classifies postings

Each poll reads `clock_timestamp()` once as `polled_at` and passes it to every statement (not `now()`, which Postgres freezes for a whole transaction). After the upsert:

| Result | How it is detected |
|---|---|
| new | `first_seen = polled_at` (the row was just inserted) |
| edited | existed, and `content_changed_at = polled_at` (the hash changed) |
| reopened | was `closed`, so `reopened_at = polled_at` |
| unchanged | everything else that was seen |
| missed | `status = open` and `last_seen < polled_at`: not in this response |

## Scenario walkthroughs

1. **First poll.** Load step inserts boards. The poller enqueues one `poll_board` job per enabled board. A worker claims one (lease set, `attempts = 1`), fetches and saves to `raw_fetches`, then in one transaction (D8): inserts every posting (`first_seen = last_seen = content_changed_at = now`, `status = open`), updates the board's observations, and marks the job `done` (fenced on the lease).
2. **Identical rerun.** The poller runs twice: the partial unique index lets only one active job per board exist. Every posting upsert hits `(board_id, external_id)`: `last_seen` moves, the hash matches, and nothing else changes. **Zero new rows** (M1 gate).
3. **One posting edited, one removed.** Edited: the hash differs, so the text, paragraphs and hash are overwritten and `content_changed_at = now`. M2 later sees that the facts' hash ≠ the posting's hash. Removed: `missed_polls` goes to 1. It closes on the 3rd consecutive miss (`status = closed`, `closed_at = now`). If it reappears later: `open`, `missed_polls = 0`, `reopened_at = now`.
4. **Worker crash mid-board.** The D8 transaction never committed, so the postings and board are untouched. The job stays `running` until its lease expires, then another worker claims it (`attempts = 2`). After `max_attempts` crashes, the job goes to `dead` with `last_error`.

## M2: extraction and matching

Implemented by `pipeline/migrations/versions/0003_extraction_and_matching.py`. Decisions D-014 to D-021.

FK rules: anything pointing at `postings` is RESTRICT (postings are closed, never deleted). `llm_runs` points at everything with SET NULL, because a cost log must outlive what it was about. `match_runs` and `matches` CASCADE from `profiles`, so deleting a profile takes its match history with it (D-020).

### posting_facts

One row per posting: the **current** extraction. Every attempt's history is in `llm_runs`.

| Column | Type | Notes |
|---|---|---|
| `posting_id` | bigint PK, FK → postings, on delete restrict | |
| `status` | text, check in (`ok`, `failed`) | `failed` = refused, truncated, or still invalid after the repair retry |
| `facts` | jsonb null | The validated `PostingFacts` exactly as the model returned them. Null when `failed`; a check enforces `status <> 'ok' OR facts IS NOT NULL` |
| `display_salary` | jsonb, default `[]` | The ranges users see (`decide_pay`): the ATS's when it publishes pay, else the model's (D-018) |
| `pay_source` | text, check in (`ats`, `text`, `none`) | Set by code, not the model |
| `pay_disagreement` | bool, default false | ATS and text ranges still differ after the one retry |
| `issues` | jsonb, default `[]` | `[{"field", "message"}]` from the last attempt; `[]` when ok |
| `content_hash` | text | The `postings.content_hash` read **before** the call. Stale when it differs from the posting's |
| `prompt_name`, `prompt_version`, `model` | text, int, text | What produced this row |
| `llm_run_id` | bigint null, FK → llm_runs, on delete set null | The last attempt's run |
| `extracted_at` | timestamptz | |

### llm_runs

One row per provider call, whatever the outcome. Evidence, not state: each row is committed in its own transaction right after the call (D-009).

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `created_at` | timestamptz | |
| `purpose` | text, check in (`extract_posting`, `extract_profile`, `rerank`) | |
| `provider` | text, check in (`anthropic`, `openai`) | |
| `model`, `prompt_name`, `prompt_version` | text, text, int | Read from the prompt file's frontmatter (D-014) |
| `posting_id` / `profile_id` / `match_run_id` | bigint null, on delete set null | Whichever the call was about |
| `attempt` | int, default 1 | 2 = the repair retry (D-017) |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | int, default 0 | For Anthropic, `input_tokens` excludes cache tokens; for OpenAI, cached tokens are a subset of input. `pricing.cost_usd` handles both |
| `cost_usd` | numeric(12,6) null | Null when the model is missing from the price table: unknown, not free |
| `latency_ms` | int | |
| `outcome` | text, check in (`ok`, `invalid`, `refused`, `truncated`, `api_error`) | D-015. `invalid` covers both a schema mismatch and an answer our validation rejected (`extract._record_verdict` updates the row after the checks run); `error` tells them apart |
| `error` | text null | Redacted by `safe_error` for sensitive calls (D-020). For extraction, `validation: <field>: <message>; ...` when our checks rejected the answer, or `pay disagreement: ...` when the answer was kept but disagreed with ATS pay. This is why a retry happened |
| `output` | jsonb null | Parsed output, or `{"raw_text": ...}` when it didn't parse. **Always null for sensitive calls** (resumes, re-rank) |
| `trace_id` | text null | Langfuse/OTel trace id (32 hex); null when tracing is off |

- **Indexes:** `(created_at)`, `(posting_id)`, `(purpose, created_at)`.

### profiles

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `name` | text unique | What the CLI uses (`make match PROFILE=me`) |
| `resume_text` | text null | Personal data. `forget-resume` sets it to null and keeps the facts |
| `facts` | jsonb | `ProfileFacts` (skills, tracks, seniority, years_experience): the LLM's draft, then whatever the user edits it to. The edited version is ground truth |
| `filters` | jsonb | `MatchFilters` (tracks, seniorities, locations, remote_ok, min_pay_usd), set by the user |
| `created_at`, `updated_at` | timestamptz | |

Matching reads only `facts` and `filters`, never `resume_text`.

### match_runs

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | |
| `profile_id` | bigint FK → profiles, on delete cascade | |
| `created_at` | timestamptz | |
| `candidates`, `passed_filters`, `reranked` | int | The funnel: considered → passed filters → re-ranked items that passed validation |
| `status` | text, check in (`ok`, `rerank_failed`) | `rerank_failed` = the feed is in stage-1 (overlap score) order |

- **Index:** `(profile_id, created_at)`.

### matches

| Column | Type | Notes |
|---|---|---|
| `match_run_id` | bigint FK → match_runs, on delete cascade | |
| `posting_id` | bigint FK → postings, on delete restrict | |
| `stage1_rank` | int | Rank by overlap score (ties: newer first) |
| `overlap_score` | real, 0..1 | D-021 |
| `final_rank` | int null | Re-ranker order; written as `stage1_rank` first, so a crash or a failed re-rank leaves the fallback order |
| `why` | text null | One sentence from the re-ranker |
| `why_evidence` | int[] | Paragraph ids backing `why`; checked against the paragraphs the model was shown |
| `skills_to_learn` | text[] | Taxonomy codes only |

- **PK:** `(match_run_id, posting_id)`. **Index:** `(posting_id)`.

### jobs changes

Extraction reuses the M1 queue (D-019).

| Change | Notes |
|---|---|
| `kind` check widened to (`poll_board`, `extract_posting`) | |
| `posting_id` bigint null, FK → postings | Set exactly when `kind = 'extract_posting'` (check: `(kind = 'poll_board') = (posting_id IS NULL)`). Extract jobs keep `board_id` = the posting's board, so `board_id` stays not null |
| `jobs_one_active_per_board_idx` dropped | It allowed one active job per `(kind, board)`, which would mean one extraction per *board* at a time |
| `jobs_one_active_poll_idx` | Unique on `(board_id)` where `kind = 'poll_board'` and status is active |
| `jobs_one_active_extract_idx` | Unique on `(posting_id)` where `kind = 'extract_posting'` and status is active |

The `ON CONFLICT` clauses in `queue.py` repeat each index's predicate word for word, or Postgres can't match them to the index. A poll job and an extract job for the same board can both be active.

### How a posting goes stale

`extract.stale_posting_ids` picks open postings where any of these holds, oldest first:

| Condition | Why |
|---|---|
| No `posting_facts` row | Never extracted |
| `posting_facts.content_hash <> postings.content_hash` | Edited since (D-006: the hash covers exactly what the model reads) |
| `posting_facts.prompt_version` differs from the newest prompt on disk | A prompt bump re-extracts everything (D-014) |

A `failed` row on the current hash and prompt version is **not** stale: the same input would cost the same and fail the same way. Closed postings are skipped. Matching only uses fresh facts (`f.content_hash = p.content_hash`).

### Extraction flow

1. **First extraction.** `make extract` enqueues one `extract_posting` job per stale posting (the partial unique index makes a rerun enqueue 0). The worker claims a job, reads the posting and its `content_hash`, renders the prompt (paragraph `[0]` is the derived header, D-016) and calls the model inside a tracing span. The `llm_runs` row commits at once. Code validates the output. In one transaction it upserts `posting_facts` (`status = ok`) and marks the job `done`, fenced on the lease.
2. **Fixable problem.** Output that fails the schema or `validate_facts` (say, a salary number not in the cited paragraph) gets one repair call (`attempt = 2`): the same prompt, plus the previous answer and the issue list. If it passes, the row is `ok`. If not, the row is `failed` with the issues, and the job is still `done` (D-017).
3. **Pay disagreement.** A valid answer whose ranges don't match the ATS's structured pay uses the same single retry, without being shown the ATS numbers. If it still disagrees: `ok`, `display_salary` = the ATS ranges, `pay_source = ats`, `pay_disagreement = true` (D-018).
4. **Refused or truncated.** `failed` at once, no retry: the same input would refuse or overflow again.
5. **API outage.** A retryable `api_error` (429, 5xx or a connection error, after the SDK's own 2 retries) writes the `llm_runs` row but no facts. The job goes to `retrying` with backoff and the posting stays stale. A non-retryable one (400, 401, unknown model) makes the job `dead`.
6. **Posting edited mid-extraction.** The facts are stored with the hash read at the start, so they are immediately stale and the next `make extract` redoes them.
7. **Lease lost.** `queue.complete` raises `LeaseLost` as the last statement of the state transaction, so the facts write rolls back. The `llm_runs` rows stay (D-009).
8. **Prompt bump.** A new `extract_posting.v2.md` makes every open posting stale. `make extract` re-extracts them all, and each row records `prompt_version = 2`.

### How matching works

`make match PROFILE=me` (D-021): candidates are open postings with fresh `ok` facts first seen in the last 30 days → code filters (D-004) → overlap score → top 20 → re-rank model (sensitive call) → validated output → `matches`. The `match_runs` and `matches` rows are written first in stage-1 order with `status = rerank_failed`; a valid re-rank then updates the ranks and flips the status to `ok`.

## Where it lives in code

| Concern | Module |
|---|---|
| ATS contract (`PostingInput`, `AtsAdapter`) | `pipeline/src/jobradar/ats/base.py` |
| Greenhouse / Ashby / Lever adapters | `ats/greenhouse.py`, `ats/ashby.py`, `ats/lever.py`, looked up via `ats/registry.py` |
| Polite HTTP (User-Agent, timeouts, outcome classes) | `ats/http.py` |
| HTML → cleaned text + numbered paragraphs, content hash | `clean.py` |
| Queue: enqueue, claim, complete, fail, backoff | `queue.py` |
| Raw response log and cache | `raw_fetches.py` |
| The per-board state transaction (upserts, misses, closes, board observations) | `ingest.py` |
| Claim → fetch → log → parse → ingest loop | `worker.py` (`make work`) |
| Enqueue due boards | `poller.py` (`make poll`) |
| Seed CSV → `boards` | `seed.py` (`make seed`) |
| Skill taxonomy (the 34 codes from `docs/SKILLS.md`) | `taxonomy.py` |
| `PostingFacts` schema (exactly the JSON the model returns) | `facts.py` |
| Validation rules, ATS pay normalization, `decide_pay` | `validate.py` |
| Prompt files (`prompts/*.vN.md`) and their frontmatter | `prompts.py` |
| Rendering the posting (header as `[0]`), taxonomy, profile and candidates for prompts | `render.py` |
| Provider contract (`LLMCall`, `LLMResult`, outcomes) | `llm/base.py` |
| Anthropic / OpenAI calls, prices, client construction | `llm/anthropic_provider.py`, `llm/openai_provider.py`, `llm/pricing.py`, `llm/registry.py` |
| One posting: call → validate → repair once → pay check → store | `extract.py` |
| Enqueue stale postings, drain extract jobs | `extract_worker.py` (`make extract`) |
| The LLM call log and cost summary | `llm_runs.py` (`make cost`) |
| OpenTelemetry spans into Langfuse | `tracing.py` |
| Profiles from a resume, plus filters | `profiles.py` (`make profile`) |
| Filters, overlap score, re-rank and its validation, feed | `match.py` (`make match`) |

## Useful queries

New Applied AI postings this week, from the extracted `track` (fresh `ok` facts only). Before M2 this matched a regex on the title:

```sql
SELECT b.company, p.title, f.facts -> 'track' -> 'value' AS track,
       f.facts -> 'seniority' ->> 'value' AS seniority, f.display_salary, p.url, p.first_seen
FROM postings p
JOIN boards b        ON b.id = p.board_id
JOIN posting_facts f ON f.posting_id = p.id
WHERE p.status = 'open'
  AND p.first_seen > now() - interval '7 days'
  AND f.status = 'ok'
  AND f.content_hash = p.content_hash
  AND f.facts -> 'track' -> 'value' ?| array['forward_deployed', 'product', 'platform']
ORDER BY p.first_seen DESC;
```

Postings edited or closed this week:

```sql
SELECT 'edited' AS change, count(*) FROM postings
WHERE content_changed_at > now() - interval '7 days' AND content_changed_at > first_seen
UNION ALL
SELECT 'closed', count(*) FROM postings WHERE closed_at > now() - interval '7 days';
```

Extraction cost per posting, by prompt version (all attempts, including repair retries):

```sql
SELECT prompt_version, model,
       count(DISTINCT posting_id) AS postings,
       count(*) AS calls,
       round(sum(cost_usd) / nullif(count(DISTINCT posting_id), 0), 6) AS cost_per_posting,
       count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_calls
FROM llm_runs
WHERE purpose = 'extract_posting' AND posting_id IS NOT NULL
GROUP BY prompt_version, model
ORDER BY prompt_version;
```

Failed extractions with their issues, one row per issue:

```sql
SELECT f.posting_id, b.company, p.title, i ->> 'field' AS field, i ->> 'message' AS message, p.url
FROM posting_facts f
JOIN postings p ON p.id = f.posting_id
JOIN boards b   ON b.id = p.board_id
CROSS JOIN LATERAL jsonb_array_elements(f.issues) AS i
WHERE f.status = 'failed'
ORDER BY f.extracted_at DESC;
```

Pay disagreements: what the text said next to what the ATS publishes (and users see):

```sql
SELECT f.posting_id, b.company, p.title,
       f.facts -> 'salary' -> 'ranges' AS text_ranges,
       f.display_salary                AS ats_ranges, p.url
FROM posting_facts f
JOIN postings p ON p.id = f.posting_id
JOIN boards b   ON b.id = p.board_id
WHERE f.pay_disagreement
ORDER BY f.extracted_at DESC;
```

## Not yet built

`feedback` and `notifications_sent` (M4, leaning toward option (c): one row per user/posting with a re-email interval). Normalized locations live only inside `posting_facts.facts`; there is no separate locations table.
