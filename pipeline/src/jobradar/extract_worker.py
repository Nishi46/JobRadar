"""The extraction worker: queue stale postings, then drain extract_posting jobs.

    python -m jobradar.extract_worker [--limit N] [--enqueue-only]

Why go through the job queue instead of looping over stale postings directly: the
queue already gives us leases, fenced commits and retry with backoff (D-001, D-008,
D-010). A crash mid-run loses nothing, two workers never extract the same posting at
once, and an API outage turns into `retrying` jobs instead of a lost run.

How outcomes map onto job states:

- extraction finished (status ok OR failed)  -> job `done`. A validation failure is a
  deterministic result, not a transient error: the posting_facts row says `failed` with
  its issues, and rerunning the same prompt would just pay to fail again. It is redone
  when the posting is edited or the prompt version changes (stale_posting_ids).
- LLM API error, retryable (429/5xx/connection, after the SDK's own retries)
                                             -> job `retrying` with backoff, no facts.
- LLM API error, not retryable (400/401...)  -> job `dead`, no facts. That's a bug or a
  config problem to look at, not something time fixes.
- lease lost                                 -> nothing recorded; the other run counts.
- a bug in our code                          -> `retrying`, then `dead` with the error.

No pause between jobs (unlike the board worker): these calls go to a paid API built
for this, not to someone's free job board.
"""

import argparse
import logging
import math
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from jobradar import queue
from jobradar.db import connect
from jobradar.extract import (
    PROMPT_NAME,
    ExtractionOutcome,
    ExtractionUnavailable,
    extract_posting,
    stale_posting_ids,
)
from jobradar.llm.base import Provider
from jobradar.llm.registry import get_provider
from jobradar.prompts import Prompt, load_prompt
from jobradar.tracing import init_tracing

log = logging.getLogger(__name__)

KIND = "extract_posting"


@dataclass(frozen=True)
class JobResult:
    job_id: int
    posting_id: int | None
    status: str  # the job's new status: done, retrying, dead, or lease_lost
    outcome: ExtractionOutcome | None  # set when the extraction finished (ok or failed)
    cost_usd: float  # what this job spent, including calls before an API error


def enqueue_stale(
    conn: psycopg.Connection[Any], *, prompt_version: int, limit: int | None = None
) -> int:
    """Queue an extract job for each stale posting (at most `limit`). Safe to rerun."""
    return queue.enqueue_extract_jobs(
        conn, stale_posting_ids(conn, prompt_version=prompt_version, limit=limit)
    )


def process_job(
    conn: psycopg.Connection[Any], provider: Provider, job: queue.Job, *, prompt: Prompt
) -> JobResult:
    """Run one claimed job to a final state for this attempt. Never raises for expected
    failures; see the module docstring for the mapping."""
    if job.posting_id is None:
        status = _fail(conn, job, "extract job without a posting_id", retryable=False)
        return JobResult(job.id, None, status, None, 0.0)
    try:
        outcome = extract_posting(conn, provider, job.posting_id, job, prompt=prompt)
    except ExtractionUnavailable as e:
        status = _fail(conn, job, str(e), retryable=e.retryable)
        return JobResult(job.id, job.posting_id, status, None, e.cost_usd)
    except queue.LeaseLost:
        log.warning("job %s: lease lost, results discarded", job.id)
        return JobResult(job.id, job.posting_id, "lease_lost", None, 0.0)
    except Exception as e:
        log.exception("job %s: unexpected error", job.id)
        detail = "".join(traceback.format_exception_only(e)).strip()
        status = _fail(conn, job, f"unexpected error: {detail}", retryable=True)
        return JobResult(job.id, job.posting_id, status, None, 0.0)

    log.info(
        "posting %s: %s after %d call(s), $%.6f, %d ms%s",
        job.posting_id,
        outcome.status,
        outcome.attempts,
        outcome.cost_usd,
        outcome.latency_ms,
        "" if not outcome.issues else f", issues: {[i.field for i in outcome.issues]}",
    )
    return JobResult(job.id, job.posting_id, "done", outcome, outcome.cost_usd)


def _fail(conn: psycopg.Connection[Any], job: queue.Job, error: str, *, retryable: bool) -> str:
    try:
        status = queue.fail(conn, job, error, retryable=retryable)
    except queue.LeaseLost:
        log.warning("job %s: lease lost while recording failure: %s", job.id, error)
        return "lease_lost"
    log.warning("job %s: %s (%s)", job.id, status, error)
    return status


def drain(
    conn: psycopg.Connection[Any],
    provider: Provider,
    worker: str,
    *,
    prompt: Prompt,
    max_jobs: int | None = None,
) -> list[JobResult]:
    """Process due extract jobs until none are left (or `max_jobs`)."""
    results: list[JobResult] = []
    while max_jobs is None or len(results) < max_jobs:
        job = queue.claim(conn, worker, kind=KIND)
        if job is None:
            break
        results.append(process_job(conn, provider, job, prompt=prompt))
    return results


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least q of the data at or
    below it. Chosen over interpolation because it always returns a latency that
    really happened, which is easier to look up in the traces."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def summarize(results: Sequence[JobResult]) -> str:
    """One line for the end of a run: counts, cost, latency."""
    outcomes = [r.outcome for r in results if r.outcome is not None]
    ok = sum(1 for o in outcomes if o.status == "ok")
    failed = len(outcomes) - ok
    not_finished = len(results) - len(outcomes)
    total_cost = sum(r.cost_usd for r in results)
    # Per posting = per finished extraction; API-error attempts are in the total only.
    per_posting = total_cost / len(outcomes) if outcomes else 0.0
    latencies = [o.latency_ms for o in outcomes]
    p50, p95 = percentile(latencies, 0.5), percentile(latencies, 0.95)
    latency = "no latency data" if p50 is None else f"latency p50 {p50:.0f} ms, p95 {p95:.0f} ms"
    return (
        f"{len(results)} jobs: {ok} ok, {failed} failed, {not_finished} not finished; "
        f"cost ${total_cost:.6f} total, ${per_posting:.6f} per posting; {latency}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract facts for stale postings, then exit.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="enqueue (and process) at most this many postings, e.g. 25 for a sample run",
    )
    parser.add_argument(
        "--enqueue-only", action="store_true", help="queue the jobs, don't run them"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    tracing_on = init_tracing()
    prompt = load_prompt(PROMPT_NAME)
    log.info(
        "prompt %s v%d (%s %s); tracing %s",
        prompt.name,
        prompt.version,
        prompt.provider,
        prompt.model,
        "on" if tracing_on else "off",
    )

    worker = queue.worker_id()
    # autocommit: every conn.transaction() block commits on its own, so llm_runs rows
    # and each posting's facts are durable as soon as they're written.
    with connect(autocommit=True) as conn:
        n = enqueue_stale(conn, prompt_version=prompt.version, limit=args.limit)
        log.info("enqueued %d extract jobs", n)
        if args.enqueue_only:
            return
        provider = get_provider(prompt.provider)
        started = time.monotonic()
        # --limit also caps processing, so leftover jobs from an earlier run can't turn
        # a 25-posting sample into an unbounded (and unbudgeted) full run.
        results = drain(conn, provider, worker, prompt=prompt, max_jobs=args.limit)
    log.info("%s; wall time %.1f s", summarize(results), time.monotonic() - started)


if __name__ == "__main__":
    main()
