"""The shared, polite HTTP layer for every ATS client.

Retry policy (G1, option c): only failures to *open a connection* are retried here,
by httpx's transport. Any response the server actually sent (429, 5xx, ...) is
returned as a FetchResult, and the job queue decides when to try again (D13), so
there is exactly one layer of backoff against a server that asked us to slow down.
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

USER_AGENT = (
    "JobRadar/0.1 (non-commercial job-matching portfolio project; "
    "+https://github.com/Nishi46/JobRadar)"
)
# Connect quickly or give up; allow longer reads since content=true bodies can be large.
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
CONNECT_RETRIES = 2


class FetchOutcome(StrEnum):
    OK = "ok"
    RETRYABLE = "retryable"  # worth another attempt later: 429, 5xx, timeouts, bad JSON
    PERMANENT = "permanent"  # retrying won't help: 4xx such as a 404 for a renamed board


@dataclass(frozen=True)
class FetchResult:
    url: str
    outcome: FetchOutcome
    http_status: int | None  # None when no response arrived
    body: Any | None  # parsed JSON; None unless the response was valid JSON
    error: str | None = None
    retry_after_s: float | None = None  # from a 429's Retry-After, seconds form only


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client with the project's User-Agent and timeouts.

    Pass `transport` only in tests (httpx.MockTransport); production uses a transport
    that retries connection failures, and nothing else.
    """
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=TIMEOUT,
        transport=transport or httpx.HTTPTransport(retries=CONNECT_RETRIES),
    )


def fetch_board(client: httpx.Client, url: str) -> FetchResult:
    """GET one board's job list. Never raises for HTTP or network problems."""
    try:
        response = client.get(url)
    except httpx.HTTPError as e:
        # Timeouts and connection errors (after the transport's connect retries).
        return FetchResult(url, FetchOutcome.RETRYABLE, None, None, f"{type(e).__name__}: {e}")

    status = response.status_code
    if status == 429 or status >= 500:
        return FetchResult(
            url,
            FetchOutcome.RETRYABLE,
            status,
            None,
            f"HTTP {status}",
            _retry_after_seconds(response),
        )
    if status != 200:
        return FetchResult(url, FetchOutcome.PERMANENT, status, None, f"HTTP {status}")

    try:
        body = response.json()
    except json.JSONDecodeError as e:
        return FetchResult(url, FetchOutcome.RETRYABLE, status, None, f"invalid JSON: {e}")
    return FetchResult(url, FetchOutcome.OK, status, body)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
