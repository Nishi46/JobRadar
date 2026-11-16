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
