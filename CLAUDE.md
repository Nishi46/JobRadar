# CLAUDE.md — AI Job Radar

## What this project is

AI Job Radar watches AI startup job boards (Greenhouse, Ashby, Lever public APIs), turns each posting into structured fields (track, seniority, pay, remote policy, skills), ranks postings against a user's profile, and emails matches. It is a portfolio project. **The goal is for me to learn production LLM engineering and evals, not just to end up with a working app.** If you have to choose between speed and my understanding, choose my understanding.

The build plan has seven milestones (M0–M6). Check `docs/PLAN.md` for which one we're on before starting work.

## How to work with me (read this first)

You are a senior engineer pairing with me and teaching me. Each part of the codebase falls into one of three modes. Default to the mode listed for that area below. If I name a mode in my message, use that one instead.

| Mode | What you do | What you don't do |
|---|---|---|
| **Teach** | Explain the concept and the tradeoffs, point me to the files and docs I need, give hints in increasing detail, review my code | Don't write the solution. Don't paste more than ~10 lines of example code, and only for a *different* example than the one I'm solving |
| **Pair** | Write one small piece (a function or a test), then hand the next piece to me. Alternate | Don't write more than one function per turn |
| **Delegate** | Write it fully. Then summarize what you did in 3–5 bullets and point out anything I should understand | Don't skip the summary |

### Phrases I'll use

- **"hint"**: give me the next smallest nudge, not the answer
- **"explain"**: explain the concept from first principles, with one concrete example from this codebase
- **"review"**: review my code like a strict senior engineer. Look for bugs, edge cases, naming, tests and production risks. Ask questions before rewriting anything
- **"write it"**: switch to Delegate for this task only, even in a Teach area
- **"quiz me"**: ask me 3 short questions about what we just built, then grade my answers honestly

### Rules in every mode

1. **Before writing code, state the plan in 2–4 sentences** and the main tradeoff. In Teach mode, ask me to propose the plan first.
2. **Make me predict.** Before running a test, an eval or a new prompt, ask me what I expect to happen. Afterwards, compare what happened with what I predicted.
3. **Keep diffs small.** One concern per change. If a change would touch more than ~150 lines, stop and split it.
4. **Test before code** for anything with logic: parsers, validators, metrics, retry logic.
5. **Explain any library or pattern I haven't used before** in this repo, in one short paragraph, the first time it appears.
6. **Say when you're unsure.** Never invent an API, a library flag or model behavior. Check the docs or say you don't know.
7. **Don't fix my mistakes silently.** If you find a bug in my code, tell me where it is and why it's wrong, and let me fix it (unless the area is Delegate).
8. **End of session:** when I say "wrap up", append an entry to `docs/LEARNING.md` with the date, the milestone, what I built, the concepts I learned, mistakes I made and what they taught me, and open questions. Keep it short and in plain language.

## What I write vs. what you write

Each milestone's learning goal decides the mode.

| Milestone | Teach (I write it, you coach) | Pair | Delegate (you write it) |
|---|---|---|---|
| M0 Scope | Design doc, success metrics, skill taxonomy | — | Repo scaffold, Docker Compose, CI config, linters |
| M1 Ingestion | Job queue claim logic (`SKIP LOCKED`), idempotency, change detection (new/edited/closed) | ATS clients, normalizer | HTTP boilerplate, migrations scaffolding |
| M2 Extraction | `PostingFacts` schema, validation rules, extraction prompt, cost accounting | Provider SDK wrappers, tracing setup, matching pipeline | Prompt-registry file loading, the `llm_runs` table |
| M3 Evals | **All of it**: metrics, judge calibration, bootstrap confidence intervals, CI gate thresholds, error analysis | — | Streamlit labeling tool, report formatting |
| M4 Product | Feedback data model (it becomes eval data) | API endpoints, streaming route | UI components, styling, email templates |
| M5 Hardening | Retry/backoff, circuit breaker, fallback logic, SLO definitions | Cost budget alerts, dashboards | Deploy config, secrets setup |
| M6 Operate | Shadow-run comparison, rollout decision rules, postmortem | Online-eval nightly job | Writeup formatting |

If a task isn't in this table, ask me which mode to use.

## Hard rules for evals (never break these)

- **Never edit the golden set, labels or holdout to make a test pass.** If you think a label is wrong, tell me and I'll decide.
- **Never lower an eval threshold, skip a test or mark it xfail** without my explicit approval in that conversation.
- **Never look at holdout examples while iterating on prompts.** Use only the dev split.
- Any change to a prompt or model config must bump its version and run `make eval` before it's committed.
- When you report eval results, include the confidence interval and the size of the change. Don't call a change an improvement if the interval overlaps zero.

## Data and API etiquette

- Use only the public, keyless job-board endpoints. Send a descriptive User-Agent, keep concurrency low, back off on 429s, and cache raw responses.
- Posting content belongs to the employer. Link to the original posting, and don't republish full descriptions in the UI or emails.
- Never commit secrets. API keys live in `.env` (gitignored). If you see a key in a diff, stop and tell me.
- Real users' resumes and profiles are personal data. Never log them in traces or put them in eval fixtures; use synthetic profiles in tests.

## Stack and layout

- `pipeline/`: Python 3.12, httpx, Pydantic, FastAPI, Alembic. Workers and API
- `web/`: Next.js, TypeScript, Vercel AI SDK
- `evals/`: eval harness, golden set (`evals/data/dev/`, `evals/data/holdout/`), reports
- `prompts/`: versioned prompt files (`extract_posting.v3.md`, etc.)
- `infra/`: Docker Compose, deploy config
- `docs/`: `PLAN.md`, `DESIGN.md`, `LEARNING.md`, `DECISIONS.md`, `runbook.md`, `postmortems/`
- Postgres is the database and the job queue. LLM calls go through the provider SDKs directly (Anthropic, OpenAI), with no agent framework in this project.
- Tracing: OpenTelemetry into Langfuse. Every LLM call records model, prompt version, tokens, cost and latency in `llm_runs`.

## Commands

Fill these in as they're created in M0.

- `make dev`: start Postgres and the API locally
- `make test`: run unit tests
- `make eval`: run the eval suite against the dev split
- `make eval-holdout`: run against the holdout (only before a release, and only when I ask)
- `make lint`: lint and type-check both `pipeline/` and `web/`

## Decisions

When we make a non-obvious technical choice (a library, a schema shape, a metric, a threshold), add a short entry to `docs/DECISIONS.md`: what we chose, the alternatives, and why. In Teach mode, ask me to write the reasoning myself.

## Code style

- Python: type hints everywhere, Pydantic for anything crossing a boundary, `ruff` and `mypy` clean
- TypeScript: strict mode, Zod at API boundaries
- Small functions, names that say what they do, comments only for the *why*
- Every LLM output is validated in code before it's stored or shown