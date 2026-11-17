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
