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

## D-001 · 2026-11-11 · Postgres as the job queue

**Context:** Ingestion needs a queue for per-board fetch jobs, with retries and backoff.
**Options considered:** Redis + a queue library (fast, extra service); a managed queue such as SQS (durable, extra cloud setup); Postgres with `SELECT … FOR UPDATE SKIP LOCKED` (no new service, transactional with the data).
**Decision:** Postgres with `SKIP LOCKED`.
**Why:** Volume is small (hundreds of boards, a few runs a day), so throughput isn't the constraint. Keeping jobs in the same database as the data means enqueueing and writing results can share a transaction. Building the claim logic by hand is also a learning goal for M1.
**Revisit if:** job volume grows past what a single Postgres instance handles comfortably, or long-running jobs start holding locks.

## D-002 · 2026-11-11 · No agent framework in this project

**Context:** LLM calls need structured outputs, retries and tracing.
**Options considered:** LangChain or LangGraph (batteries included, more abstraction); raw provider SDKs (more code, full visibility).
**Decision:** Raw Anthropic and OpenAI SDKs.
**Why:** This project has no multi-step agent, so a framework adds abstraction without much payoff. Calling the APIs directly teaches what frameworks hide (schemas, retries, token accounting) and makes provider fallback something I build rather than configure. LangGraph is saved for Project 2, where an agent loop justifies it.
**Revisit if:** the matching step grows into a multi-step agent.

<!-- New decisions go above D-002, newest first -->
