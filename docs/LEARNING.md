# Learning Log

One entry per work session, newest first. Claude appends an entry when you say "wrap up" (see CLAUDE.md). Edit it so it's in your words: this log is your interview-prep material.

**Format**

```
## YYYY-MM-DD · M# · <one-line summary>

**Built:** what exists now that didn't before
**Learned:** concepts, in plain language, as if explaining to an interviewer
**Mistakes:** what went wrong and what it taught me
**Predictions vs. reality:** what I expected a test/eval/prompt to do, and what happened
**Open questions:** things to look up or ask next time
**Interview story candidate?** yes / no — if yes, one sentence on the story
```

---

<!-- Entries go below this line, newest first -->

## 2026-11-21 · M1 · Ashby and Lever clients, cleaner, queue, worker, poller: M1 done

**Mode:** Delegate (I asked Claude to write it; four parts were built in parallel by agents against one shared contract).
**Built:** `PostingInput`, the one shape every ATS adapter returns. Ashby and Lever adapters next to Greenhouse. The HTML cleaner (numbered paragraphs plus the content hash). The Postgres queue (claim with `SKIP LOCKED`, lease, fenced complete/fail, equal-jitter backoff). `raw_fetches` logging with hash dedupe. The per-poll state transaction (`ingest.py`), the worker and the poller. `make poll`, `make work`, `make ingest`. Decisions D-010 to D-013.
**Results:** The first real run covered 165 boards and 4,668 postings with every job `done`. An immediate rerun gave 0 new, 0 edited and 4,668 unchanged postings, and stored 165 hashes and no bodies. That is the M1 gate, on real data.
**Learned:**
- *A shared contract makes parallel work possible.* Writing `PostingInput` and the adapter signature first let three adapters and the worker be built at the same time without stepping on each other. Adding a fourth ATS is one module.
- *`now()` is frozen for a whole transaction.* It is the transaction's start time, so "was this row touched by this poll?" can't use it. Each poll reads `clock_timestamp()` once and compares rows against that `polled_at`.
- *SKIP LOCKED:* a second worker skips rows another transaction has locked instead of waiting for them, so concurrent workers each get a different job.
- *The fence token is (locked_by, attempts),* not just the worker name. Even the same worker reclaiming its own expired job can't commit with the stale claim.
- *Where a crash gets noticed:* a crashed worker can't record its own failure, so the next claim buries an expired job that has no attempts left.
- *Dedupe is the cache.* Storing a body only when its canonical-JSON hash changes keeps every fetch on record for the cost of a hash.
- *Real APIs differ from their docs.* Ashby puts `compensation` on each job, not at the top level, and sends an empty compensation object rather than null when pay is hidden. Lever's `lists` content often isn't `<li>` items.
**Mistakes:** _(fill in: what would you have done differently if you'd written it?)_
**Predictions vs. reality:** _(fill in: before reading the numbers, what did you expect the rerun to report, and how big did you think `raw_fetches` would be? It was about 28 MB per full set of distinct bodies, 1.9 MB for the largest board, and `postings` about 92 MB.)_
**Open questions:**
- Ashby sends a compensation object even when pay is hidden. Should M3 treat an empty `compensationTiers` as "no ground truth"? (All 3,343 Ashby postings have a non-null `ats_compensation` today.)
- Lever's `salaryDescription` holds pay text the extractor may need, but it is only in `ats_raw` today. Should it go into `description_html`? Adding it would change those postings' hashes.
- Should a posting that becomes unlisted on Ashby close? It does today, after 3 polls.
- Retention for `raw_fetches` against Neon's 0.5 GB: re-measure after a week of hourly runs.
**Interview story candidate?** yes: the "zero duplicates" gate is enforced by the database (a unique constraint, a partial unique index and a fenced commit), not by Python checks. It was proven by a rerun over 4,668 real postings.

## 2026-11-17 · M1 · Data model, seed load, polite HTTP and the Greenhouse client

**Built:** The M1 tables (`boards`, `postings`, `jobs`, `raw_fetches`) in migration 0002, documented in `DATA_MODEL.md`. A load step that upserts the seed CSV into `boards`. A shared HTTP layer that sends a descriptive User-Agent and sorts every response into ok, retryable or permanent. A Greenhouse client that validates a board and keeps each job's raw JSON. Decisions D-005 to D-009.
**Learned:**
- *Idempotency belongs in the database, not in Python.* A "does it exist?" check races when two pollers run at once. A unique constraint plus `INSERT … ON CONFLICT DO UPDATE` makes Postgres the referee, and a rerun becomes the normal update path (D-005).
- *Hash what feeds the model, store what feeds the UI.* "Edited" should mean "the extraction might change," so the content hash covers only the cleaned title, locations and text. Store `cleaner_version` alongside it, so a mass hash change can be explained (D-006).
- *Absence is the only signal for "closed," and it's noisy.* Wrongly closing a live posting is an error nobody sees, while staleness costs only a few hours. So a posting closes after 3 missed *successful* polls, and failed fetches don't count (D-007).
- *Leases rescue crashed jobs without a reaper.* The claim query treats an expired `running` job as claimable, `attempts` goes up at claim time so a job that keeps crashing ends up dead, and the final commit is fenced on the lease so a slow worker can't overwrite newer results (D-008).
- *Evidence logs go outside the state transaction.* `raw_fetches` matters most when something fails, so it can't sit in a transaction that those failures roll back (D-009).
- *One layer of backoff.* The HTTP layer retries only connection failures. Any response the server actually sent goes back to the queue, which decides when to retry.
- *If one job in a board is malformed, fail the whole board.* Dropping just that job would count as a missed poll and could eventually close a live posting.
- *Config and observations have different owners.* The CSV owns board config. The poller owns `last_polled_at` and similar columns, and a reload never touches them (D16).
**Mistakes:** Two Nov 16 decisions didn't survive the Nov 17 walkthrough. (1) Closing after *2* missed polls became *3*, once I priced out that wrongly closing a live posting is invisible while staleness costs only hours. (2) The content hash first included ATS pay; now it covers only what the LLM reads (title, locations, cleaned text), with pay stored untouched in `ats_compensation`. Lesson: a decision written before the schema exists is a draft, and walking through concrete scenarios (rerun, edit, crash) is what exposes the gaps. I also committed `docs/DECISIONS.pdf` by accident and removed it in the "M0 finished" commit, so check `git status` before committing.
**Predictions vs. reality:** None recorded. I didn't predict test results before running them this session. Next time, before running the seed upsert twice, write down the expected row count and whether `updated_at` should move.
**Open questions:** How large are real `content=true` responses, and what does that mean for `raw_fetches` retention on Neon's free tier? Is a 10-minute lease right once real board run times are measured?
**Interview story candidate?** yes: the lease plus fenced commit, and why a too-short lease would double-count `missed_polls` and close postings early.

## 2026-11-16 · M0 · Final design, seed list and scaffold

**Built:** The final design doc, the repo scaffold (Docker Compose Postgres, Alembic, CI with lint, types and tests), and a seed list of about 170 companies tagged with ATS, board token and stage.
**Learned:** Seed data needs verifying before it's trusted: the `verified` flag became `enabled`, and boards on out-of-scope ATSs (Gem, Rippling, custom) are left out entirely. Board tokens are stored exactly as the API needs them, because Ashby is case-sensitive.
**Mistakes:** I handed Teach-mode work to Claude. "Answer the design questions" and "write it" meant the first versions of the edited/closed decisions were Claude's reasoning, not mine, even though CLAUDE.md says Teach-area "Why" lines are mine to write. The decisions were sound, but the point of this project is that I can defend them in an interview. I also ran a lot of work through parallel subagents (seed list and scaffold at once), and `/usage` showed general-purpose subagents were 51% of my usage. Parallel agents are worth it for big independent chunks, not by default.
**Predictions vs. reality:** None recorded.
**Open questions:** When is delegating a Teach-area task worth losing the learning? (Pick a rule, e.g. delegate only when I can already explain the decision.)
**Interview story candidate?** no

## 2026-11-11 · M0 · Scoping: design choices before any prompt

**Built:** The first design doc and decisions D-001 to D-004.
**Learned:**
- *Postgres as the queue* (D-001): at this volume throughput doesn't matter. Keeping jobs and data in one database lets them share a transaction, and building `SKIP LOCKED` by hand is a learning goal in itself.
- *No agent framework* (D-002): nothing here is a multi-step agent, so raw SDKs teach what frameworks hide (schemas, retries, token accounting).
- *Model the data the way the world gives it* (D-003): postings publish several location-linked pay ranges, so `salary_ranges` is a list, and each range is scored separately so a wrong city can't hide behind a right one.
- *Permissive filters, strict ranking* (D-004): a posting that is wrongly filtered out is a miss nobody sees, while a borderline posting that gets through produces feedback. Precision is enforced at ranking, where it can be measured.
**Mistakes:** None recorded; this session's transcript isn't saved.
**Predictions vs. reality:** None recorded.
**Open questions:** How will "unknown" abstentions be scored in M3 so the model can't use them to slip past the filters (D-004's revisit condition)?
**Interview story candidate?** yes: D-004, choosing which errors stay visible so that feedback can catch them.
