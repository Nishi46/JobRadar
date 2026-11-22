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

## 2026-11-21 · M2 · Extraction, validation, providers, tracing, profiles and matching: code complete

**Mode:** Delegate (six parallel agents building against one written contract: schema + validation, providers, migration + queue + `llm_runs` + tracing, prompts + rendering, profiles + matching, the extraction pipeline).
**Built:** Migration 0003 (`posting_facts`, `llm_runs`, `profiles`, `match_runs`, `matches`, and `extract_posting` jobs on the same queue). The `PostingFacts` schema and the 34-code taxonomy. `validate.py` (citations, taxonomy, salary numbers in the cited text, ATS pay normalization, `decide_pay`). Anthropic and OpenAI providers with a closed set of outcomes and per-call cost. Versioned prompt files with the model in the frontmatter (`extract_posting.v1`, `extract_profile.v1`, `rerank.v1`). OpenTelemetry spans for Langfuse. The extraction pipeline and worker (one bounded retry, fenced commit). Profiles from a resume, and two-stage matching. `make extract`, `make profile`, `make match`, `make cost`. Decisions D-014 to D-021.
**Results:** 548 tests pass with every provider faked, and ruff and mypy strict are clean. **Nothing has run against a real API yet** (no keys in `.env`), so there are no real numbers on cost, failure rate or quality.
**Learned:**
- *Structured outputs are a request, not a guarantee, so validate in code.* The schema constrains the JSON's shape, but only code can check that paragraph 14 exists, that "180000" appears in it, and that `EVALS` is in the taxonomy. Code checks every posting for free; an eval only samples.
- *Grounding by citation that code checks.* The model cites paragraph ids, never free-text quotes, so a check can look up the paragraph and confirm the number is there. This makes an invented salary impossible to store, not just unlikely. The header is paragraph 0 so ATS-only facts (locations, remote) are citable, but numbers can't cite it (D-016).
- *Why the SDK's parse helpers were bypassed.* `messages.parse` and `responses.parse` raise on bad or truncated output and lose both the text and the token usage. The repair retry needs the text, and cost accounting needs the usage of calls that failed. So we call `create` with the SDK's own schema converter and validate ourselves (D-015).
- *Model tiering.* A small model reads thousands of postings against a fixed schema; a stronger model ranks 20 per user and writes the "why." Putting the model in the prompt file makes a model change a version bump, so the eval gate covers it (D-014).
- *One bounded repair retry.* Show the model its previous answer and the list of problems, once. That caps the worst case at two calls per posting. A posting still wrong after that is a `failed` row with its issues, which is error-analysis data for M3 (D-017).
- *Cost accounting per call from day one.* Every call, including refused and failed ones, gets an `llm_runs` row with tokens, cost and latency, committed outside the state transaction (D-009), so a rollback can't erase what we paid for.
- *Keep ATS pay an independent label.* Users see the ATS pay when it exists, but the model never sees it, not even in the disagreement retry. If it did, it would copy the numbers and M3 couldn't use ATS pay to score salary extraction. "ATS has pay, text has none" isn't a disagreement, because the retry would pressure the model to invent a salary (D-018).
- *Personal data in traces.* Resume and re-rank calls are flagged `sensitive`: no text in `llm_runs.output` or spans, and errors are redacted because a validation error quotes the output. The SDK's built-in OTel is turned off so it can't leak content around our rule (D-020).
**Teach areas that were delegated:** CLAUDE.md lists the `PostingFacts` schema, validation rules, the extraction prompt, retry logic and cost accounting as Teach areas for M2, and all of them were written by agents. Before M3, walk through `pipeline/src/jobradar/extract.py`, `pipeline/src/jobradar/validate.py` and `prompts/extract_posting.v1.md` yourself (say "quiz me" or "explain"), and rewrite the D-014 to D-021 "Why" lines in your own words where you disagree.
**Mistakes:** _(fill in: what would you have designed differently if you'd written the schema, prompt and validator yourself?)_
**Predictions vs. reality:** _(fill in before the first `make extract LIMIT=25`: what share will be `ok`, which issue field will fail most, what cost per posting, and will `cache_read_tokens` be non-zero? Then compare.)_
**Open questions:**
- **Labeling rules made in the prompt that you must confirm**, because the M3 golden-set labels have to follow the same rules or the evals measure a disagreement about rules, not extraction quality: a span of levels ("Senior/Staff") takes the lowest; with no stated level, years decide it (under 2 junior, 2–4 mid, 5–7 senior, 8+ staff_plus); one-sided pay ("up to $250K") is skipped; "must be authorized to work in the US" on its own is `unstated` visa; track `other` means any role that isn't AI engineering, including engineering that doesn't build AI systems.
- Location matching is a case-insensitive substring match, so a user location "US" matches "Austin, TX" (the "us" in "Austin"), and "CA" matches "Canada" and "Chicago". Short or region-level user locations need real normalization, or exact matching on the normalized values.
- The $5,000 pay tolerance is in dollars, so every hourly range agrees with every other within $5K/hr. Hourly needs its own tolerance.
- Ashby monthly pay (and bonus, equity) is dropped from the ATS ranges. How common is it on these boards?
- A salary listed across several paragraphs (one range per city, each in its own `<li>`) fails the single-citation check (D-003's revisit condition). How many real postings do this?
- `skills_to_learn` is the posting's required skills minus the profile's skills: plain set arithmetic. Should code compute it instead of the re-rank model?
- The re-rank is estimated at $0.08–0.10 per user per run, over DESIGN's ≤ $0.05 per user per week. Measure it, then decide: fewer candidates, a smaller input, or a cheaper model.
- Each attempt is its own Langfuse trace; there's no parent span per posting tying attempt 1 and the repair retry together. Add one before M3 error analysis?
**Interview story candidate?** yes: "every salary number must appear in the paragraph the model cites" turns the worst extraction error (an invented salary) from a rate an eval estimates into something code rules out, and the ATS pay is kept away from the model so it stays an honest label.

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
