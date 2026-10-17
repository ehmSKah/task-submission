"""How the counter adapter fails, and how fast (ADR 0006).

Floci can neither throttle a key nor fail on cue, so these tests need no emulator: service errors
come from botocore's `Stubber`, and timeouts from local sockets that never answer, which is what
an unreachable or hanging DynamoDB looks like to the client.
"""

import socket
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.outbound.dynamodb_rate_limits import (
    CALL_BUDGET_SECONDS,
    CLIENT_CONFIG,
    DynamoDbRateLimitCounters,
)
from orders_api.application import fail_open_counters
from orders_api.application.fail_open_counters import FAILURES_TO_OPEN
from orders_api.application.rate_limiting import CounterStoreThrottled, CounterStoreUnavailable
from orders_api.domain.access import Role

EXPIRES_AT = datetime(2026, 10, 17, 9, 33, tzinfo=UTC)
KEY = "ip#203.0.113.7#2026-10-17T09:31Z"


def stubbed() -> tuple[DynamoDbRateLimitCounters, Stubber]:
    session = boto3.session.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"
    )
    table = session.resource("dynamodb", config=CLIENT_CONFIG).Table("rate-limits")
    stubber = Stubber(table.meta.client)
    stubber.activate()
    return DynamoDbRateLimitCounters("rate-limits", table=table), stubber


def count(counters: DynamoDbRateLimitCounters) -> int | None:
    return counters.increment(KEY, limit=5, expires_at=EXPIRES_AT)


@pytest.mark.parametrize("code", ["ProvisionedThroughputExceededException", "ThrottlingException"])
def test_a_throttled_key_is_reported_as_throttled(code: str) -> None:
    counters, stubber = stubbed()
    stubber.add_client_error("update_item", service_error_code=code, http_status_code=400)

    with pytest.raises(CounterStoreThrottled):
        count(counters)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("InternalServerError", 500),
        ("ServiceUnavailable", 503),
        ("RequestLimitExceeded", 400),  # the account's throughput, not one client's key
    ],
)
def test_a_service_error_is_reported_as_unavailable(code: str, status: int) -> None:
    counters, stubber = stubbed()
    stubber.add_client_error("update_item", service_error_code=code, http_status_code=status)

    with pytest.raises(CounterStoreUnavailable) as raised:
        count(counters)

    assert raised.value.error_type == code


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "ResourceNotFoundException", "ValidationException"]
)
def test_our_own_defects_are_no_outage_and_propagate(code: str) -> None:
    """A missing permission or table, or a malformed request, is a deploy or code defect: it
    fails loudly instead of quietly turning distributed limits into per-instance ones."""
    counters, stubber = stubbed()
    stubber.add_client_error("update_item", service_error_code=code, http_status_code=400)

    with pytest.raises(ClientError):
        count(counters)


def test_the_client_settings_fit_one_call_into_the_budget() -> None:
    """One attempt, no retries: a failed connect plus a failed read can't exceed the budget."""
    timeouts = CLIENT_CONFIG.connect_timeout + CLIENT_CONFIG.read_timeout

    assert timeouts <= CALL_BUDGET_SECONDS
    assert CLIENT_CONFIG.retries == {"mode": "standard", "total_max_attempts": 1}


PointAt = Callable[[tuple[str, int]], None]


@pytest.fixture
def point_at(monkeypatch: pytest.MonkeyPatch) -> PointAt:
    """Point new AWS clients at a local address, as the deployed function is pointed at its
    region's endpoint, with dummy credentials."""

    def point(address: tuple[str, int]) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL", f"http://{address[0]}:{address[1]}")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    return point


@pytest.fixture
def silent_server() -> Iterator[tuple[str, int]]:
    """Accepts connections and never answers: a read that times out."""
    with socket.create_server(("127.0.0.1", 0), backlog=8) as server:
        yield server.getsockname()


@pytest.fixture
def full_server() -> Iterator[tuple[str, int]]:
    """Never accepts, and its queue is full, so new connections hang: a connect that times out,
    like an address that drops packets."""
    with socket.create_server(("127.0.0.1", 0), backlog=0) as server:
        fillers = [socket.socket() for _ in range(4)]
        for filler in fillers:
            filler.setblocking(False)
            filler.connect_ex(server.getsockname())
        time.sleep(0.05)
        yield server.getsockname()
        for filler in fillers:
            filler.close()


@pytest.fixture
def closed_port() -> tuple[str, int]:
    """Nothing listens: the connection is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        address: tuple[str, int] = probe.getsockname()
    return address


@pytest.mark.parametrize(
    ("server", "error_type"),
    [
        ("silent_server", "ReadTimeoutError"),
        ("full_server", "ConnectTimeoutError"),
        ("closed_port", "EndpointConnectionError"),
    ],
)
def test_a_store_that_doesn_t_answer_is_unavailable_within_the_budget(
    server: str, error_type: str, point_at: PointAt, request: pytest.FixtureRequest
) -> None:
    point_at(request.getfixturevalue(server))
    counters = DynamoDbRateLimitCounters("rate-limits")

    started = time.perf_counter()
    with pytest.raises(CounterStoreUnavailable) as raised:
        count(counters)
    elapsed = time.perf_counter() - started

    assert raised.value.error_type == error_type
    assert elapsed < CALL_BUDGET_SECONDS


def test_the_api_function_lets_requests_through_while_the_counter_table_hangs(
    silent_server: tuple[str, int],
    closed_port: tuple[str, int],
    point_at: PointAt,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The function's own wiring, with every AWS call hanging: the limits come from the defaults
    (the parameter can't be read) and the counts from memory; no request waits longer than one
    counter call's budget, and once the circuit is open, not at all."""
    point_at(silent_server)
    # The parameter read isn't what this test times: it is refused at once, and not retried.
    monkeypatch.setenv("AWS_ENDPOINT_URL_SSM", f"http://{closed_port[0]}:{closed_port[1]}")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    # A shorter rest after a failure, still covering the rest of its request, keeps this quick.
    monkeypatch.setattr(fail_open_counters, "REST_AFTER_FAILURE_SECONDS", 0.3)
    monkeypatch.setenv("RATE_LIMIT_TABLE", "rate-limits")
    monkeypatch.setenv("RATE_LIMITS_PARAMETER", "/orders-api/test/rate-limits")
    monkeypatch.setenv("RATE_LIMITS_MAX_AGE_SECONDS", "600")
    limiter = api_handler.rate_limiter.__wrapped__()

    durations, decisions = [], []
    for _ in range(FAILURES_TO_OPEN):
        time.sleep(0.3)  # each request asks the store once: its IP check fails, its user rests
        started = time.perf_counter()
        decisions.append(limiter.check("203.0.113.7", "alice-sub", frozenset({Role.USER})))
        durations.append(time.perf_counter() - started)
    started = time.perf_counter()
    decisions.append(limiter.check("203.0.113.7", "alice-sub", frozenset({Role.USER})))
    open_duration = time.perf_counter() - started

    assert all(decision.allowed for decision in decisions)
    assert max(durations) < CALL_BUDGET_SECONDS
    assert open_duration < 0.01
