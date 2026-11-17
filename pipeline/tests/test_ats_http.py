from collections.abc import Callable

import httpx
import pytest

from jobradar.ats.http import USER_AGENT, FetchOutcome, fetch_board, make_client

URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
Handler = Callable[[httpx.Request], httpx.Response]


def client_returning(handler: Handler) -> httpx.Client:
    # MockTransport answers requests in-process: no network in unit tests.
    return make_client(transport=httpx.MockTransport(handler))


def test_ok_response_returns_parsed_body() -> None:
    client = client_returning(lambda r: httpx.Response(200, json={"jobs": []}))

    result = fetch_board(client, URL)

    assert result.outcome is FetchOutcome.OK
    assert result.http_status == 200
    assert result.body == {"jobs": []}
    assert result.error is None


def test_sends_descriptive_user_agent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jobs": []})

    fetch_board(client_returning(handler), URL)

    assert seen[0].headers["user-agent"] == USER_AGENT
    assert "github.com" in USER_AGENT


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_and_server_errors_are_retryable(status: int) -> None:
    client = client_returning(lambda r: httpx.Response(status, text="busy"))

    result = fetch_board(client, URL)

    assert result.outcome is FetchOutcome.RETRYABLE
    assert result.http_status == status
    assert result.body is None


def test_retry_after_seconds_is_captured_on_429() -> None:
    client = client_returning(
        lambda r: httpx.Response(429, headers={"Retry-After": "120"}, text="slow down")
    )

    assert fetch_board(client, URL).retry_after_s == 120.0


def test_unparseable_retry_after_is_ignored() -> None:
    # Retry-After may also be an HTTP date; we only honor the seconds form.
    client = client_returning(
        lambda r: httpx.Response(429, headers={"Retry-After": "Wed, 18 Nov 2026 07:28:00 GMT"})
    )

    assert fetch_board(client, URL).retry_after_s is None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_permanent(status: int) -> None:
    # e.g. 404: the board token is wrong or the company left this ATS. Retrying won't help.
    client = client_returning(lambda r: httpx.Response(status, text="nope"))

    result = fetch_board(client, URL)

    assert result.outcome is FetchOutcome.PERMANENT
    assert result.error is not None
    assert str(status) in result.error


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = fetch_board(client_returning(handler), URL)

    assert result.outcome is FetchOutcome.RETRYABLE
    assert result.http_status is None
    assert result.error is not None
    assert "ReadTimeout" in result.error


def test_connection_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert fetch_board(client_returning(handler), URL).outcome is FetchOutcome.RETRYABLE


def test_200_with_invalid_json_is_retryable() -> None:
    # A CDN error page served with 200 is usually transient; the queue's attempt limit
    # sends it to `dead` if it keeps happening.
    client = client_returning(lambda r: httpx.Response(200, text="<html>oops</html>"))

    result = fetch_board(client, URL)

    assert result.outcome is FetchOutcome.RETRYABLE
    assert result.body is None
    assert result.error is not None
