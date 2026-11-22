"""The extraction worker: enqueue stale postings, drain extract_posting jobs, map outcomes
onto job states. The LLM is a fake Provider; Postgres is real (rolled back)."""

from typing import Any

import pytest

from extract_fakes import (
    Conn,
    FakeProvider,
    bad_facts,
    facts_row,
    good_facts,
    llm_result,
    make_board,
    make_posting,
    scalar,
)
from jobradar import queue
from jobradar.extract_worker import (
    JobResult,
    drain,
    enqueue_stale,
    percentile,
    process_job,
    summarize,
)
from jobradar.prompts import Prompt, load_prompt

# Only the DB tests start from empty tables; the percentile tests need no database.
db_test = pytest.mark.usefixtures("empty_tables")


@pytest.fixture
def prompt() -> Prompt:
    return load_prompt("extract_posting")


@pytest.fixture
def posting_id(db: Conn, empty_tables: None) -> int:
    return make_posting(db, make_board(db))


def claim(db: Conn) -> queue.Job:
    job = queue.claim(db, "test-worker", kind="extract_posting")
    assert job is not None
    return job


def job_status(db: Conn, job: queue.Job) -> Any:
    return scalar(db, "SELECT status FROM jobs WHERE id = %s", job.id)


@db_test
def test_enqueue_stale_is_safe_to_rerun(db: Conn, posting_id: int, prompt: Prompt) -> None:
    assert enqueue_stale(db, prompt_version=prompt.version) == 1
    # Still queued: the active-job index makes the second enqueue a no-op.
    assert enqueue_stale(db, prompt_version=prompt.version) == 0


@db_test
def test_rerun_with_fresh_facts_enqueues_nothing(db: Conn, posting_id: int, prompt: Prompt) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    drain(db, FakeProvider([llm_result("ok", good_facts())]), "w", prompt=prompt)

    assert enqueue_stale(db, prompt_version=prompt.version) == 0


@db_test
def test_enqueue_limit_caps_the_sample(db: Conn, prompt: Prompt) -> None:
    board = make_board(db)
    for i in range(5):
        make_posting(db, board, external_id=str(i))

    assert enqueue_stale(db, prompt_version=prompt.version, limit=3) == 3


@db_test
def test_ok_extraction_marks_the_job_done(db: Conn, posting_id: int, prompt: Prompt) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)

    result = process_job(db, FakeProvider([llm_result("ok", good_facts())]), job, prompt=prompt)

    assert result.status == "done"
    assert result.outcome is not None and result.outcome.status == "ok"
    assert job_status(db, job) == "done"


@db_test
def test_validation_failure_is_done_not_retried(db: Conn, posting_id: int, prompt: Prompt) -> None:
    # Deterministic: running the same prompt again would cost money and fail the same way.
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)
    provider = FakeProvider([llm_result("ok", bad_facts()), llm_result("ok", bad_facts())])

    result = process_job(db, provider, job, prompt=prompt)

    assert result.status == "done"
    assert result.outcome is not None and result.outcome.status == "failed"
    assert job_status(db, job) == "done"
    row = facts_row(db, posting_id)
    assert row is not None and row["status"] == "failed"


@db_test
def test_retryable_api_error_retries_the_job_and_writes_no_facts(
    db: Conn, posting_id: int, prompt: Prompt
) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)
    provider = FakeProvider([llm_result("api_error", error="HTTP 529", retryable=True)])

    result = process_job(db, provider, job, prompt=prompt)

    assert result.status == "retrying"
    assert job_status(db, job) == "retrying"
    assert "529" in scalar(db, "SELECT last_error FROM jobs WHERE id = %s", job.id)
    assert facts_row(db, posting_id) is None
    assert result.cost_usd == pytest.approx(0.001)  # paid-for attempts still count


@db_test
def test_non_retryable_api_error_kills_the_job(db: Conn, posting_id: int, prompt: Prompt) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)
    provider = FakeProvider([llm_result("api_error", error="HTTP 400", retryable=False)])

    assert process_job(db, provider, job, prompt=prompt).status == "dead"
    assert facts_row(db, posting_id) is None


@db_test
def test_lease_lost_is_reported_and_writes_nothing(
    db: Conn, posting_id: int, prompt: Prompt
) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)
    db.execute(
        "UPDATE jobs SET locked_by = 'other', attempts = attempts + 1 WHERE id = %s", (job.id,)
    )

    result = process_job(db, FakeProvider([llm_result("ok", good_facts())]), job, prompt=prompt)

    assert result.status == "lease_lost"
    assert facts_row(db, posting_id) is None


@db_test
def test_a_bug_in_our_code_is_a_retryable_failure(
    db: Conn, posting_id: int, prompt: Prompt
) -> None:
    enqueue_stale(db, prompt_version=prompt.version)
    job = claim(db)

    result = process_job(db, FakeProvider([]), job, prompt=prompt)  # raises AssertionError

    assert result.status == "retrying"
    assert scalar(db, "SELECT last_error FROM jobs WHERE id = %s", job.id).startswith(
        "unexpected error"
    )


@db_test
def test_drain_processes_every_job_and_respects_max_jobs(db: Conn, prompt: Prompt) -> None:
    board = make_board(db)
    for i in range(3):
        make_posting(db, board, external_id=str(i))
    enqueue_stale(db, prompt_version=prompt.version)
    ok = [llm_result("ok", good_facts()) for _ in range(3)]

    first = drain(db, FakeProvider(ok[:1]), "w", prompt=prompt, max_jobs=1)
    rest = drain(db, FakeProvider(ok[1:]), "w", prompt=prompt)

    assert (len(first), len(rest)) == (1, 2)
    assert scalar(db, "SELECT count(*) FROM jobs WHERE status = 'done'") == 3
    assert scalar(db, "SELECT count(*) FROM posting_facts WHERE status = 'ok'") == 3


# --- the run summary ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([100], 0.95, 100),
        ([1, 2, 3, 4], 0.5, 2),  # nearest rank: ceil(0.5 * 4) = 2nd value
        ([4, 1, 3, 2], 0.95, 4),  # unsorted input is fine
        (list(range(1, 101)), 0.95, 95),
    ],
)
def test_percentile_is_nearest_rank(values: list[int], q: float, expected: int) -> None:
    assert percentile(values, q) == expected


def test_percentile_of_nothing_is_none() -> None:
    assert percentile([], 0.5) is None


@db_test
def test_summarize_counts_costs_and_latency(db: Conn, prompt: Prompt) -> None:
    board = make_board(db)
    for i in range(2):
        make_posting(db, board, external_id=str(i))
    enqueue_stale(db, prompt_version=prompt.version)
    provider = FakeProvider(
        [
            llm_result("ok", good_facts(), cost_usd=0.002, latency_ms=300),
            llm_result("refused", error="no", cost_usd=0.001, latency_ms=100),
        ]
    )
    results: list[JobResult] = drain(db, provider, "w", prompt=prompt)

    text = summarize(results)

    assert "1 ok" in text
    assert "1 failed" in text
    assert "$0.003000 total" in text
    assert "$0.001500 per posting" in text
    assert "p50 100 ms" in text
    assert "p95 300 ms" in text
