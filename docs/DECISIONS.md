# Decision Log

Every non-obvious technical choice: a library, a schema shape, a metric, a threshold, a model. Newest first. In Teach-mode areas, write the "Why" yourself.

**Format**

```
## D-### · YYYY-MM-DD · <decision in a few words>

**Context:** what problem forced a choice
**Options considered:** A, B, C (one line each on the tradeoff)
**Decision:** what we picked
**Why:** the reasoning, in 2–4 sentences
**Revisit if:** the signal that would make us change our minds
```

---

## D-013 · 2026-11-21 · Poller runs hourly; a board is due 6 hours after its last successful poll

**Context:** The plan says to poll every 6 hours. A cron job that runs exactly every 6 hours drifts against `last_polled_at` (a board polled at 00:00:30 isn't "6 hours old" at 06:00), and a laptop that sleeps through one run waits another 6 hours.
**Options considered:** Cron every 6 hours with a "due after 5.5 hours" slack; cron hourly with "due after 6 hours"; a long-running scheduler process.
**Decision:** Cron runs `make ingest` hourly. The poller enqueues only enabled boards whose `last_polled_at` is null or more than 6 hours old (`poller.POLL_INTERVAL`).
**Why:** Running more often than the interval costs nothing, because boards that aren't due are skipped and enqueueing is idempotent. A missed run is caught up within an hour. Real spacing ends up between 6 and 7 hours, which is within the design's ≤ 8-hour freshness target. A failing board keeps its old `last_polled_at`, so it stays due and is retried once its previous job is dead.
**Revisit if:** we deploy to a host with a real scheduler in M5, or freshness needs to be tighter than 7 hours.

## D-012 · 2026-11-21 · Raw responses are deduplicated by hash, which also makes them the cache

**Context:** R6 says to cache raw responses, and D-009 says to log every fetch. One full set of distinct bodies for 165 boards is about 28 MB (measured), and most boards don't change between polls.
**Options considered:** Store every body (simple, but about 28 MB per run, more than 100 MB a day hourly); store nothing on success (loses the evidence D-009 exists for); store a body only when it differs from the board's last stored body.
**Decision:** Every fetch gets a `raw_fetches` row. The body is hashed as canonical JSON (sorted keys, no whitespace); if the hash equals the board's latest stored body's hash, only `body_sha256` is stored and `body` stays NULL.
**Why:** Every poll stays on record (status, error, and which content it saw) for the cost of a hash when nothing changed. Canonical JSON means a reordered key isn't a "new" body. The unchanged rerun on 2026-11-21 stored 165 hashes and 0 bodies.
**Revisit if:** some boards return volatile fields (timestamps in every response) so the hash never matches; then hash only the parts we parse.

## D-011 · 2026-11-21 · How descriptions become numbered paragraphs

**Context:** M2 has the LLM cite paragraph IDs, and code checks those citations (for example, a salary number must appear in the cited paragraph). The cleaner's choices decide how precise and how checkable a citation can be.
**Options considered:** Split on blank lines in a text dump (loses list structure); one paragraph per block element; a third-party HTML library such as BeautifulSoup or selectolax.
**Decision:** Stdlib `HTMLParser`. Each block element is a paragraph, and so is each `<li>` (prefixed with `- `) and each table row (cells joined with ` | `). A single `<br>` also ends a paragraph. IDs are 1-based positions. Text is unescaped and whitespace collapsed, and characters are never reworded, so `$180,000 – $220,000` keeps its en dash. Zero-width characters are removed. `content_hash` is sha256 over a JSON list of the whitespace-normalized title, the **sorted** locations and the cleaned text. `CLEANER_VERSION` is stored next to the hash, not inside it.
**Why:** Smaller paragraphs make citations precise: "the salary is in paragraph 14" beats "somewhere in this 400-word block." Many postings use `<br>` instead of `<p>`, so treating it as a break avoids giant paragraphs. Sorting locations stops an ATS reordering them from looking like an edit. Keeping the version out of the hash means a cleaner bump only marks the postings whose text actually changed as edited, so M2 doesn't pay to re-extract identical input. No new dependency is needed for ATS fragment HTML.
**Revisit if:** M2 citations often need two adjacent paragraphs (splitting too fine), or `<pre>` and code blocks turn out to matter (whitespace is collapsed today). The M2 prompt must show locations in the same sorted order the hash uses.

## D-010 · 2026-11-21 · Queue retry policy: equal-jitter backoff, Retry-After honored, dead on reclaim

**Context:** D-008 defined leases and the fenced commit. Still open: how long to wait between attempts, what to do with a server's `Retry-After`, and what happens to a job whose lease expired on its last attempt.
**Options considered:** Fixed delay; exponential delay with no jitter (boards that failed together retry together); exponential delay with full jitter (can retry almost immediately); exponential delay with equal jitter.
**Decision:** The delay is `60 s × 2^(attempts−1)`, capped at 1 hour, with equal jitter (half fixed, half random). A `Retry-After` in seconds sets a floor, itself capped at 6 hours. `max_attempts = 5`, and a 4xx other than 429 goes straight to `dead`. An expired `running` job that has no attempts left is marked `dead` by the claim instead of being run again. The fence token is `(locked_by, attempts)`.
**Why:** Equal jitter spreads retries out but keeps a floor, so we never hammer a server that just failed. The server knows its own rate limit better than we do, so its `Retry-After` wins, but a bogus value can't park a job for a year. Burying at claim time is what turns "the worker crashes on this board every time" into a visible `dead` row instead of an endless loop. Including `attempts` in the fence means even the same worker, reclaiming its own expired job, can't commit with the stale claim.
**Revisit if:** dead jobs show up for boards that recover on their own (raise `max_attempts`), or M5's provider retries need different numbers; they get their own constants.

## D-009 · 2026-11-17 · Evidence logs are written outside the state transaction

**Context:** Every board fetch is saved to `raw_fetches` (R6). D-008 puts the postings writes and "mark job done" in one transaction that rolls back on any failure. The question is whether the raw fetch belongs inside that transaction.
**Options considered:** Inside (atomic with the postings, but a crash during processing rolls the response away, and failed fetches never reach that transaction at all); before it, in its own committed transaction (every fetch is recorded, at the cost of an extra commit and occasional rows with no matching postings writes).
**Decision:** The worker writes the `raw_fetches` row right after the fetch, for every outcome (ok, retryable, permanent), and commits it before parsing and the state transaction begin.
**Why:** `raw_fetches` is a log of what the server said, and it matters most when something went wrong: reproducing a parser crash, or checking whether a posting that closed was really missing. A log inside the state transaction gets erased by exactly the failures it exists to explain. Atomic transactions are for state that must stay consistent; append-only evidence is written immediately and independently. The same rule applies to `llm_runs` in M2.
**Revisit if:** raw fetch storage grows faster than retention can trim it.

## D-008 · 2026-11-17 · Queue jobs hold a lease, and the commit is fenced on it

**Context:** A worker claims a job, sets it to `running`, then gets killed (OOM, deploy, Ctrl-C). Nothing ever moves that row out of `running`, so the board silently stops being polled.
**Options considered:** Hold one transaction open across claim, fetch and write (Postgres rolls back on crash, but holds a connection and lock during network I/O and hides the `running` state); a separate reaper process that resets stuck jobs (one more thing to run); a lease checked by the claim query itself.
**Decision:** Claiming sets `locked_by`, `locked_at` and `lease_expires_at` (about 10 minutes) and increments `attempts`. The claim query treats a `running` job with an expired lease as claimable. The final "mark done" step in the per-board transaction only succeeds if this worker still holds the lease; otherwise the whole transaction rolls back.
**Why:** The next worker looking for work rescues stuck jobs, so nothing extra has to run. The lease is many times longer than a real board run (seconds), because a lease that is too short lets two workers process the same board; upserts survive that, but `missed_polls` would be counted twice and postings would close early. Fencing the commit stops a slow worker from writing stale results over a newer run. Counting attempts at claim time means a board that crashes the worker every time ends up `dead` instead of looping forever.
**Revisit if:** real board runs approach the lease length, or `dead` jobs show up with no `last_error` (crashes we never see).

## D-007 · 2026-11-17 · A posting closes after 3 consecutive missed polls

**Context:** "Closed" is the only state inferred from absence: a posting just stops appearing. A timeout, a 429 or a glitchy empty response can look like every posting vanished.
**Options considered:** Close as soon as a posting is missing (fast, but one bad response closes a whole board); close when the drop is under a percentage threshold (fires constantly on small boards, where one closure is 11%); close after N consecutive misses in successful polls.
**Decision:** A posting closes after missing 3 consecutive successful polls, tracked by a `missed_polls` counter that resets whenever it is seen. Failed fetches (timeouts, 429s after retries) don't count as polls. An empty `{"jobs": []}` response does count, but the first time a non-empty board comes back empty, I get an alert. A posting that reappears goes back to `open` with `reopened_at` set; `first_seen` never changes.
**Why:** The two errors aren't equal. Closing an open posting is invisible: a real job disappears from feeds and nobody notices. Keeping a closed posting one poll too long costs about 6 hours of staleness, and the user sees "position filled" if they click. So I accept 12–18 hours of delay to make one-off glitches harmless. An empty board still counts so a company that freezes hiring or moves ATS eventually closes; the alert gives me 12 hours to check before it does.
**Revisit if:** postings often reopen right after closing (N is too small), or users complain about stale postings (N is too large).

## D-006 · 2026-11-17 · Content hash covers only what the LLM reads

**Context:** Change detection needs to tell "edited" from "unchanged". Every edit triggers re-extraction in M2, so a false edit costs money.
**Options considered:** Hash the full raw JSON (simple, but Greenhouse compliance flags, new API fields or key order would mark every posting edited at once); trust the ATS's `updated_at` (its triggers aren't documented); hash a chosen set of cleaned fields.
**Decision:** The hash covers the cleaned title, locations and description text. Departments, offices, timestamps and compliance fields are stored (in `ats_raw`) but not hashed. A `cleaner_version` is stored next to the hash. Postings keep only the current text; golden-set and feedback records snapshot the text or its hash when they are made.
**Why:** "Edited" should mean "the extraction might come out differently", so the hash covers exactly the extraction's inputs: hash what feeds the model, store what feeds the UI. Hashing cleaned text means escaping changes or stray whitespace aren't edits. The catch is that changing the cleaner changes every hash, which is why the version is stored. Snapshotting only labels and feedback, rather than versioning every posting, protects the evals (labels can't drift when an employer edits a posting) without adding a version join to every query.
**Revisit if:** extractions differ between postings with the same hash (a field that matters isn't hashed), or I want the history of how postings changed over time.

## D-005 · 2026-11-17 · Posting identity is (ats, board_token, external_id)

**Context:** Reruns of the poller must create zero duplicates (M1 gate), including when two pollers run at the same moment.
**Options considered:** `external_id` alone (an ID is only unique within the system that issued it); `(board_token, external_id)` (works today only because no two ATSs in the seed list share a token); `(ats, board_token, external_id)`.
**Decision:** `(ats, board_token, external_id)`, enforced as a database unique constraint (stored as `(board_id, external_id)`, where `board_id` stands for one `(ats, board_token)`). Writes use `INSERT … ON CONFLICT DO UPDATE`. A company that moves to a different ATS gets new postings, and the old ones close.
**Why:** Each ATS names its boards independently, so the key has to include the system that issued the ID. A check in Python can't prevent duplicates: two pollers can both see "doesn't exist" and both insert. The constraint makes the database the referee, and `ON CONFLICT` turns the conflict into the normal update path, since after the first poll almost every write hits an existing row. Linking postings across an ATS move isn't worth the complexity for how rarely it happens.
**Revisit if:** companies switching ATS turns out to be common enough to break history on the market page.

## D-004 · 2026-11-11 · Permissive filters, strict ranking

**Context:** Matching applies hard filters (track, seniority, location, pay) in code, but most of the filtered fields come from LLM extraction and can be `unknown` or wrong. Postings with several salary ranges, and users with several acceptable locations, also need a rule for the pay filter.
**Options considered:** Strict filters (drop anything `unknown` or borderline: high precision, but misses that nobody ever sees); permissive filters with strict ranking (let borderline postings through and rely on the re-ranker and precision@5 to keep them out of the top 5).
**Decision:** Permissive filters, strict ranking. A field extracted as `unknown` passes its filter. The pay filter compares the user's minimum against the range for the user's location; if no range matches, it uses the highest range. With several user locations, the posting passes if *any* matching range meets the minimum. Hourly ranges are annualized at 2,080 hours; OTE is compared against the minimum as is. A posting passes the track filter if any of its tracks matches; `unknown` passes, `other` does not.
**Why:** A posting that is wrongly filtered out is a miss nobody ever sees, so no metric or feedback catches it. A borderline posting that gets through is visible: the user can mark it "not relevant," and that becomes eval data. Precision, which §6 of the design doc says matters more, is enforced at the ranking stage instead of the filter. The highest-range fallback follows the same rule. When no range matches the user's location, the pay that applies to them is unknown, and the highest published range is the most it could plausibly be. Filtering on it therefore drops only postings that miss the minimum under every published range; the lowest range would also drop postings that might qualify, and nobody would see those misses. The cost is some matches whose real pay is below the minimum. That cost is visible: the card shows every range with its location, so the user sees the real numbers before clicking and can mark the match "not relevant."
**Revisit if:** precision@5 misses its target because borderline postings reach the top 5, the model's `unknown` rate climbs (abstaining becomes a way to slip past the filters), or remote users often mark highest-range-fallback matches "not relevant." In that last case, switch the fallback to the lowest range.

## D-003 · 2026-11-11 · Salary as a list of location-linked ranges

**Context:** Many Series B/C postings publish several pay ranges by location (e.g. SF, NYC, remote), and some give OTE or hourly pay. A single `salary_min`/`salary_max` pair can't represent them.
**Options considered:** A single range (simple, but wrong for every city except one); a primary range chosen by the model or by code (still one number, and "primary" is a judgment call that's hard to label); all ranges, each linked to a location (more schema and more scoring decisions, but faithful to the posting).
**Decision:** `salary_ranges` is a list of {`location`, `min`, `max`, `currency`, `pay_type`}. Each `location` must appear in the posting's `locations`, and one paragraph citation covers the whole list.
**Why:** Pay is the field users check first, so showing a range that doesn't apply to them is a trust-breaking error. Keeping every range avoids picking the wrong one. Linking each range to a normalized location lets code check the extraction (the location must exist in `locations`) and lets the pay filter use the range that applies to the user (see D-004). The cost is that scoring gets harder; we score each range separately and count missing and extra ranges, so one wrong city can't hide behind a right one.
**Revisit if:** ranges often span several paragraphs (the single citation breaks the numbers-in-text check), or location normalization proves unreliable enough that ranges can't be matched to users.


## D-002 · 2026-11-11 · No agent framework in this project

**Context:** LLM calls need structured outputs, retries and tracing.
**Options considered:** LangChain or LangGraph (batteries included, more abstraction); raw provider SDKs (more code, full visibility).
**Decision:** Raw Anthropic and OpenAI SDKs.
**Why:** This project has no multi-step agent, so a framework adds abstraction without much payoff. Calling the APIs directly teaches what frameworks hide (schemas, retries, token accounting) and makes provider fallback something I build rather than configure. LangGraph is saved for Project 2, where an agent loop justifies it.
**Revisit if:** the matching step grows into a multi-step agent.

## D-001 · 2026-11-11 · Postgres as the job queue

**Context:** Ingestion needs a queue for per-board fetch jobs, with retries and backoff.
**Options considered:** Redis + a queue library (fast, extra service); a managed queue such as SQS (durable, extra cloud setup); Postgres with `SELECT … FOR UPDATE SKIP LOCKED` (no new service, transactional with the data).
**Decision:** Postgres with `SKIP LOCKED`.
**Why:** Volume is small (hundreds of boards, a few runs a day), so throughput isn't the constraint. Keeping jobs in the same database as the data means enqueueing and writing results can share a transaction. Building the claim logic by hand is also a learning goal for M1.
**Revisit if:** job volume grows past what a single Postgres instance handles comfortably, or long-running jobs start holding locks.

<!-- New decisions go above D-001, newest first -->
