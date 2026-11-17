# Data model (M1: ingestion)

Sketch of the M1 tables.
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
| `kind` | text | `poll_board` for now; M2 extraction jobs can reuse the table |
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

- **Claimable:** (`status in (queued, retrying)` and `run_after <= now()`) or (`status = running` and `lease_expires_at < now()`).
- **Index:** `(status, run_after)` for the claim query.
- **at most one active job per board**, as a partial unique index on `(kind, board_id)` where `status in (queued, retrying, running)`. This makes the poller's enqueue idempotent ("safe to run twice", PLAN M1) with `ON CONFLICT DO NOTHING`, the same pattern as D1.

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

- Retention (not enforced yet): keep 14 days, plus the latest fetch per board. **Open:** measure real response sizes first. Greenhouse with `content=true` can be large, and Neon's free tier is small.
- Doubles as the debugging record when a posting closes unexpectedly: "was it really missing from the response?"

## Scenario walkthroughs

1. **First poll.** Load step inserts boards. The poller enqueues one `poll_board` job per enabled board. A worker claims one (lease set, `attempts = 1`), fetches and saves to `raw_fetches`, then in one transaction (D8): inserts every posting (`first_seen = last_seen = content_changed_at = now`, `status = open`), updates the board's observations, and marks the job `done` (fenced on the lease).
2. **Identical rerun.** The poller runs twice: the partial unique index lets only one active job per board exist. Every posting upsert hits `(board_id, external_id)`: `last_seen` moves, the hash matches, and nothing else changes. **Zero new rows** (M1 gate).
3. **One posting edited, one removed.** Edited: the hash differs, so the text, paragraphs and hash are overwritten and `content_changed_at = now`. M2 later sees that the facts' hash ≠ the posting's hash. Removed: `missed_polls` goes to 1. It closes on the 3rd consecutive miss (`status = closed`, `closed_at = now`). If it reappears later: `open`, `missed_polls = 0`, `reopened_at = now`.
4. **Worker crash mid-board.** The D8 transaction never committed, so the postings and board are untouched. The job stays `running` until its lease expires, then another worker claims it (`attempts = 2`). After `max_attempts` crashes, the job goes to `dead` with `last_error`.

## Not in M1

`posting_facts` and `llm_runs` (M2), `feedback` and `notifications_sent` (M4, leaning toward option (c): one row per user/posting with a re-email interval).
