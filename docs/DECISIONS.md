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

## D-021 · 2026-11-21 · Matching: permissive filters as built, a posting-normalized overlap score, and a re-rank checked in code

**Context:** D-004 set the filter rules in words. Building them raised cases it didn't cover: a `remote_policy` of `unknown` with no location match, pay in other currencies, which display value the pay filter reads, and what to do when the re-ranker's output is partly wrong.
**Options considered:** For the score: plain count of shared skills (favors postings with long skill lists); Jaccard over profile and posting skills (penalizes a profile with many extra skills); share of the posting's skills the profile has, with preferred skills weighted lower. For a bad re-rank: fail the whole run; drop the bad items and keep the rest; fall back to stage-1 order.
**Decision:** Filters (`match.passes_filters`): `unknown` passes every filter. Location passes on a case-insensitive substring match either way, or when the user is `remote_ok` and the posting is remote or its `remote_policy` is `unknown`. Pay reads `display_salary` (what the card shows), only USD ranges count (no exchange rates), hourly × 2,080, and a range passes when its *top* reaches the minimum. The score is `(|required ∩ P| + 0.5 · |preferred ∩ P|) / (|required| + 0.5 · |preferred|)`, 0 when the posting lists no skills; the top 20 by score (newer first on ties) go to the re-ranker. Its output is validated item by item: the posting must be a candidate and appear once, the `why` must be non-empty, every evidence id must be a paragraph the model was shown for that posting, and a `skills_to_learn` code outside the taxonomy is dropped on its own. If the call fails or no item survives, the run is `rerank_failed` and the feed keeps stage-1 order; candidates the model left out follow the re-ranked ones in stage-1 order.
**Why:** Every filter choice errs toward letting a posting through, for D-004's reason: a wrong exclusion is invisible and a wrong inclusion produces feedback. Normalizing by the posting makes the score read as "how much of what they ask for do I have," so a short skills list doesn't win by accident and an experienced profile isn't penalized. Dropping single bad items keeps most of a mostly-good re-rank, and a citation the model was never shown can't be grounded, so it can't stand. The stage-1 fallback means a provider outage degrades the feed's order instead of emptying it.
**Revisit if:** precision@5 in M3 shows filter leaks reaching the top 5, substring location matching produces visibly wrong matches (see the open question in `LEARNING.md`), or re-rank drops are frequent enough that the fallback is the normal path.

## D-020 · 2026-11-21 · Resume and re-rank calls are sensitive: counts in logs and traces, never text

**Context:** Profile extraction sends a resume, and the re-rank prompt contains the user's profile facts. CLAUDE.md says personal data must never land in traces or eval fixtures, but `llm_runs` and Langfuse exist precisely to keep inputs and outputs for debugging.
**Options considered:** Trace everything and scrub later (the data has already left); keep resume calls out of `llm_runs` and tracing entirely (loses their cost and failure record); log those calls with their text removed.
**Decision:** `LLMCall.sensitive = True` for `extract_profile` and `rerank`. For those calls `llm_runs.output` is NULL, spans carry no input or output attributes, and `safe_error` withholds every error except API errors, because a Pydantic validation error quotes the model's output. Token counts, cost, latency and outcome are kept. The Anthropic SDK's own OTel spans are disabled (D-015). Deleting a profile cascades to its `match_runs` and `matches`; its `llm_runs` rows survive with `profile_id` / `match_run_id` set to NULL. `forget-resume` deletes the stored text and keeps the facts.
**Why:** Cost accounting and failure rates need every call, but none of them need the text. Enforcing the rule in one place (`tracing.py` and `llm_runs.py` read the flag) means a caller can't forget it. The re-rank's useful output (`why`, `skills_to_learn`) is stored in `matches` anyway, so little is lost. Cascading the match history but keeping the cost rows means deleting a user removes what they told us and keeps only what the calls cost.
**Revisit if:** debugging a bad re-rank needs its full input; then add an opt-in for my own profile only, not a general switch.

## D-019 · 2026-11-21 · Extraction runs on the same Postgres queue as polling

**Context:** Extraction needs what polling needed: retry with backoff on API errors, no two workers on one posting, and a crash that loses nothing.
**Options considered:** A plain loop over stale postings (simplest, but a crash or outage loses the run and retries are hand-rolled); a second queue table; the existing `jobs` table with a new `kind`.
**Decision:** `jobs.kind = 'extract_posting'` with a `posting_id` column, and one active job per posting (`jobs_one_active_extract_idx`). The old per-`(kind, board)` index is split into one index per kind. `make extract` enqueues every stale posting and drains the jobs; the `posting_facts` write and the fenced `queue.complete` share one transaction.
**Why:** This is the payoff of D-001: leases, the fenced commit and equal-jitter backoff (D-008, D-010) came for free, and a rate-limit storm becomes `retrying` jobs instead of a lost run. The partial unique index makes enqueueing idempotent, the same pattern as polling. Keeping `board_id` on extraction jobs means every existing query that joins jobs to boards still works.
**Revisit if:** extraction volume needs real concurrency (several workers with provider rate limits shared between them), or extraction jobs crowd out polling.

## D-018 · 2026-11-21 · ATS pay cross-check: ATS pay is displayed, and the retry never sees it

**Context:** DESIGN §5 says to compare the model's salary with the ATS's structured pay, retry once on a disagreement, then display the ATS value. It doesn't say what "agree" means, what to do when only the ATS has pay, or how to read Ashby's tiers.
**Options considered:** For agreement: exact match; within a tolerance. For "ATS has pay, text has none": a disagreement; not a disagreement. For the retry: show the model the ATS numbers; only ask it to re-read.
**Decision:** `validate.decide_pay` displays the ATS ranges whenever the ATS publishes pay (`pay_source = ats`). They agree when every ATS range has a model range in the same currency within $5,000 on both min and max (DESIGN §6's salary tolerance); extra model ranges are fine and locations aren't compared. ATS pay with no text ranges is **not** a disagreement. Ashby: only `Salary` components with a yearly or hourly interval count (no equity, bonus or commission, no monthly pay), and the tier's `title` is used as the range's location ("" when empty). Ashby's empty compensation object for hidden pay becomes None. The disagreement retry asks the model to re-read its cited paragraph and never shows the ATS numbers.
**Why:** The ATS value is structured data the employer typed in, so it can't be misread, which makes it the right thing to show. Calling "ATS only" a disagreement would spend the retry asking the model to find a salary that isn't in the text, which is pressure to invent one, the error DESIGN §6 cares most about. Keeping the ATS numbers out of the retry keeps them an independent label: if the model saw them it would copy them, and M3 could no longer use ATS pay to score salary extraction. Summing bonus or equity into a range would produce a number the employer never wrote.
**Revisit if:** M3 shows the $5,000 tolerance is wrong for hourly ranges (it makes almost any two hourly ranges agree) or Ashby tier titles are rarely places, so the pay filter almost always falls back to the highest range.

## D-017 · 2026-11-21 · One retry per posting, and which failures the queue retries

**Context:** An extraction can fail in different ways: schema-invalid output, output that breaks a validation rule, a pay disagreement, a refusal, truncation at `max_tokens`, or an API error. Each needs a rule for whether to retry, and whether the failure belongs to the posting or to the job.
**Options considered:** Retry until valid (unbounded cost); one retry per failure type (up to three calls); one retry in total.
**Decision:** At most one retry per posting, so at most two calls. Schema-invalid or validation issues get a repair retry (the original prompt plus the previous output and the issue list, "fix these, keep everything else"). If the first answer is valid, a pay disagreement can use the retry instead (D-018). Refused and truncated outputs fail with no retry. The results map onto state like this:

| Result | `posting_facts` | Job |
|---|---|---|
| ok (after 1 or 2 calls) | `ok` | `done` |
| still invalid after the retry, refused, truncated | `failed` with `issues` | `done`: deterministic, not retried by the queue |
| retryable `api_error` (429, 5xx, connection, after the SDK's 2 retries) | no row change | `retrying` with backoff |
| non-retryable `api_error` (400, 401, unknown model) | no row change | `dead` |
| lease lost | rolled back | the other worker's run counts |

Every call's `llm_runs` row is written outside the state transaction (D-009), in all of these cases.
**Why:** One retry bounds the worst case at about twice the median cost per posting, so the backfill cost is predictable. A posting still wrong after a targeted repair is usually wrong for a reason a third call won't fix (an odd posting or a weak prompt), and its `failed` row with issues is exactly what M3 error analysis wants to read. Refusals and truncation would repeat on the same input. An outage says nothing about the posting, so it mustn't mark it failed; the queue retries it once the API recovers.
**Revisit if:** the repair retry rarely fixes anything (then it's wasted money), truncation shows up (raise `max_tokens` in a new prompt version), or failed rows pile up for one issue type.

## D-016 · 2026-11-21 · The posting header is paragraph 0: citable for most fields, never for numbers

**Context:** Locations, remote policy and often seniority are stated only by the job board (title, ATS location and department fields, Ashby's workplace type), not in the description. Every field needs a paragraph citation (D-011), so without an id for that information the model can't cite it and validation rejects honest answers.
**Options considered:** Exempt those fields from citations (weakens grounding); store the header as a real paragraph in `postings.paragraphs` (changes what the content hash covers); render it as a derived paragraph 0 on every call.
**Decision:** `render.header_paragraph` builds `[0]` from the company, title, ATS locations (sorted, as in the hash), departments and ATS hints. It is shown to the model and passed to `validate_facts`, but not stored. It may be cited for track, seniority, locations and remote policy, but never for `salary` or `years_min`, which must cite the description paragraph where the number appears (enforced in `validate.py`). ATS compensation is never a hint.
**Why:** Paragraph 0 keeps every field citable without exempting any, and validation sees exactly what the model saw. Numbers are excluded because a header's digits ("5 openings", a street address) would let an invented salary pass the numbers-in-text check. The accepted cost: departments and ATS hints are in the prompt but not in `content_hash`, so a change to only those leaves the facts looking fresh. The title and locations, which matter most, are hashed.
**Revisit if:** M3 error analysis finds facts that are wrong because a department or workplace-type change didn't trigger re-extraction.

## D-015 · 2026-11-21 · Providers call `create` and validate in our code, and never raise

**Context:** Both SDKs have structured-output helpers (`messages.parse` for Anthropic, `responses.parse` for OpenAI) that return a parsed Pydantic object. Reading their source showed they raise on output that doesn't fit the schema, including a response truncated at `max_tokens`, and the exception carries neither the raw text nor the token usage.
**Options considered:** Use the parse helpers and catch the exception (loses the text and the cost of a call we paid for); call `create` with the schema and validate ourselves.
**Decision:** Providers call `messages.create` / `responses.create` with the schema built by the SDK's own converter (`anthropic.transform_schema`, OpenAI's `type_to_response_format_param`), so the request is identical to what `parse` sends, then validate with Pydantic in our code. `Provider.complete` never raises: every call returns an `LLMResult` with outcome `ok`, `invalid` (text came back but failed the schema; `raw_text` kept), `refused`, `truncated` or `api_error` (with `retryable`). The SDKs' own retries (2) handle 429s and 5xx; there is no second retry layer. The Anthropic SDK's built-in OpenTelemetry is turned off (`open_telemetry=False`).
**Why:** The repair retry needs the invalid text to show the model what it got wrong, and cost accounting needs the usage of every call, including the failed ones. A closed set of outcomes means callers handle failures with a `match` on a value instead of guessing which exceptions an SDK throws. SDK spans would duplicate every generation in Langfuse next to ours, and they could carry prompt content, which would break the resume rule (D-020).
**Revisit if:** an SDK release makes the parse helpers return text and usage on failure, or we need streaming.

## D-014 · 2026-11-21 · Model tiering: Haiku for extraction, a stronger OpenAI model for re-rank, set in the prompt file

**Context:** Extraction runs on every posting (4,668 today, plus every edit), while the re-rank runs on 20 candidates per user per run. PLAN M2 asks for a cheap model for volume and a stronger one for the few re-ranked results, and DESIGN sets budgets of ≤ $0.01 per posting and ≤ $0.05 per user per week.
**Options considered:** One model for everything (simpler, but either expensive at volume or weak at ranking); a small model for extraction and a stronger one for re-rank; the model chosen in code or config, or in the prompt file.
**Decision:** Extraction and profile extraction use Anthropic `claude-haiku-5-5` ($0.10 in / $0.50 out per MTok, cache read $0.01) with effort `low`. The system prompt (instructions plus the taxonomy, about 2.2K tokens) is marked cacheable, so every call after the first reads it at a tenth of the price; the backfill of 4,668 postings is estimated at $3–5. Re-rank uses OpenAI `gpt-6.2-sol` ($2 in / $10 out per MTok), which was my choice so both providers are used for real. Provider, model, `max_tokens` and effort live in each prompt's frontmatter, so changing a model is a prompt version bump and re-extracts everything.
**Why:** Extraction is mostly careful reading against a fixed schema, which a small model can do when code checks every answer. Ranking and writing a grounded "why" is the judgment call, and it runs on 20 items, so a stronger model costs little in total. Putting the model in the prompt file means `llm_runs` and `posting_facts` always know exactly which prompt *and* model produced a row, and M3's "a prompt or model change must pass the evals" rule covers both. **Open:** the re-rank estimate is about $0.08–0.10 per user per run, above DESIGN's ≤ $0.05 per week. That is unmeasured until the first live run, and fixes (a smaller re-rank input, a cheaper model, fewer candidates) wait for real numbers. Whether the system prompt is long enough to be cached at all is also unconfirmed until `cache_read_tokens` shows up in `llm_runs`.
**Revisit if:** measured extraction cost exceeds $0.01 per posting, the re-rank stays over budget, or M3 shows Haiku's field accuracy below target.

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
