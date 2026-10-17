"""Rate limiting in the request pipeline: step 3, after identity, before request checks
(ADRs 0006, 0009 and 0012)."""

import io
import json
from typing import Any

import pytest

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api.application.fail_open_counters import (
    FAILURES_TO_OPEN,
    OPEN_SECONDS,
    REST_AFTER_FAILURE_SECONDS,
)
from orders_api.application.rate_limiting import CounterStoreThrottled, CounterStoreUnavailable
from orders_api.domain.access import Role
from orders_api.domain.rate_limits import RateLimits
from tests.builders import an_order, rest_event
from tests.unit.events import (
    ALICE,
    MANAGER,
    NOBODY,
    change_status_event,
    create_order_event,
    delete_order_event,
    get_order_event,
    headers_of,
    invoke,
    list_orders_event,
    problem_of,
)
from tests.unit.fakes import (
    FailingRateLimitCounters,
    InMemoryOrderRepository,
    RateLimitsInUse,
    UnreliableRateLimitCounters,
    WireApi,
)

ORDER = an_order(owner_id="alice-sub")
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"
NEW_ID = "0e6bb2c6-0000-4000-8000-0000000000aa"
WINDOW = "2026-10-17T09:30Z"  # the unit tests' fixed clock
TINY = RateLimits(per_ip=10, per_role={Role.USER: 2, Role.ORDER_MANAGER: 4, Role.ADMIN: 4})
TINY_IP = RateLimits(per_ip=2, per_role={Role.USER: 10, Role.ORDER_MANAGER: 10, Role.ADMIN: 10})
RATE_LIMIT_HEADERS = ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset")


@pytest.fixture(autouse=True)
def repository(wire_api: WireApi) -> InMemoryOrderRepository:
    return wire_api([ORDER], ids=[NEW_ID])


def rate_limit_headers(response: dict[str, Any]) -> dict[str, str]:
    headers = headers_of(response)
    return {name: headers[name] for name in RATE_LIMIT_HEADERS if name in headers}


def test_a_successful_response_names_the_more_restrictive_limit(
    rate_limits: RateLimitsInUse,
) -> None:
    response = invoke(get_order_event(ORDER.id))

    assert response["statusCode"] == 200
    assert rate_limit_headers(response) == {
        "RateLimit-Limit": "60",
        "RateLimit-Remaining": "59",
        "RateLimit-Reset": "60",
    }
    assert "Retry-After" not in headers_of(response)


def test_every_order_route_is_counted_against_one_budget(rate_limits: RateLimitsInUse) -> None:
    events = [
        create_order_event(),
        get_order_event(ORDER.id),
        list_orders_event(),
        change_status_event(ORDER.id, status="CANCELLED", authorizer=ALICE),
        delete_order_event(ORDER.id, authorizer=ALICE),
    ]

    remaining = [rate_limit_headers(invoke(event))["RateLimit-Remaining"] for event in events]

    assert remaining == ["59", "58", "57", "56", "55"]
    assert rate_limits.counters.counts[f"user#alice-sub#{WINDOW}"] == 5
    assert rate_limits.counters.counts[f"ip#203.0.113.7#{WINDOW}"] == 5


def test_over_the_user_limit_is_a_429_naming_the_scope(rate_limits: RateLimitsInUse) -> None:
    rate_limits.config.limits = TINY
    for _ in range(2):
        invoke(get_order_event(ORDER.id))

    response = invoke(get_order_event(ORDER.id))

    body = problem_of(response, 429, "rate-limited")
    assert body["scope"] == "user"
    assert body["detail"] == (
        "Too many requests for this user: the limit is 2 per minute. Retry after 60 seconds."
    )
    assert body["instance"] == f"/api/v1/orders/{ORDER.id}"
    headers = headers_of(response)
    assert headers["Retry-After"] == "60"
    assert rate_limit_headers(response) == {
        "RateLimit-Limit": "2",
        "RateLimit-Remaining": "0",
        "RateLimit-Reset": "60",
    }
    for name, value in SECURITY_HEADERS.items():
        assert headers[name] == value


def test_over_the_ip_limit_is_a_429_naming_the_scope(rate_limits: RateLimitsInUse) -> None:
    rate_limits.config.limits = TINY_IP
    invoke(get_order_event(ORDER.id, authorizer=ALICE))
    invoke(get_order_event(ORDER.id, authorizer=MANAGER))

    response = invoke(list_orders_event(authorizer=NOBODY))

    body = problem_of(response, 429, "rate-limited")
    assert body["scope"] == "ip"
    assert body["detail"].startswith("Too many requests from this IP address: the limit is 2")
    assert rate_limit_headers(response)["RateLimit-Limit"] == "2"
    assert f"user#nobody-sub#{WINDOW}" not in rate_limits.counters.counts


def test_a_rejected_request_never_reaches_the_use_case(
    rate_limits: RateLimitsInUse, repository: InMemoryOrderRepository
) -> None:
    rate_limits.config.limits = TINY
    for _ in range(2):
        invoke(create_order_event())

    response = invoke(change_status_event(ORDER.id, status="CANCELLED", authorizer=ALICE))

    assert response["statusCode"] == 429
    assert repository.updates_attempted == 0


@pytest.mark.parametrize(
    "event",
    [
        create_order_event(raw="{not json"),
        create_order_event(content_type="text/plain"),
        get_order_event("not-a-uuid"),
        list_orders_event({"limit": "0"}),
        get_order_event(ORDER.id, authorizer=NOBODY),
    ],
    ids=["invalid-body", "wrong-content-type", "malformed-id", "bad-query", "no-permission"],
)
def test_the_limit_is_checked_before_the_request_and_the_permission(
    rate_limits: RateLimitsInUse, event: dict[str, Any]
) -> None:
    """Malformed or forbidden requests spend the budget too, so they are no way around it."""
    rate_limits.config.limits = RateLimits(
        per_ip=10, per_role={Role.USER: 1, Role.ORDER_MANAGER: 1, Role.ADMIN: 1}
    )
    invoke(event)

    problem_of(invoke(event), 429, "rate-limited")


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (get_order_event(UNKNOWN_ID), 404),
        (get_order_event("not-a-uuid"), 400),
        (list_orders_event(authorizer=NOBODY), 403),
        (change_status_event(ORDER.id, status="SHIPPED"), 409),
        (create_order_event(content_type="text/plain"), 415),
    ],
)
def test_errors_after_the_check_carry_the_headers_too(
    rate_limits: RateLimitsInUse, event: dict[str, Any], status: int
) -> None:
    response = invoke(event)

    assert response["statusCode"] == status
    assert set(rate_limit_headers(response)) == set(RATE_LIMIT_HEADERS)


def test_health_is_not_limited(rate_limits: RateLimitsInUse) -> None:
    rate_limits.config.limits = TINY_IP

    responses = [invoke(rest_event("GET", "/api/v1/health")) for _ in range(5)]

    assert [response["statusCode"] for response in responses] == [200] * 5
    assert rate_limit_headers(responses[-1]) == {}
    assert rate_limits.counters.counts == {}


def test_a_request_without_identity_is_never_counted(rate_limits: RateLimitsInUse) -> None:
    response = invoke(get_order_event(ORDER.id, authorizer=None))

    assert response["statusCode"] == 500
    assert rate_limit_headers(response) == {}
    assert rate_limits.counters.counts == {}


def test_the_client_ip_comes_from_the_gateway_never_from_x_forwarded_for(
    rate_limits: RateLimitsInUse,
) -> None:
    event = get_order_event(ORDER.id)
    event["headers"]["X-Forwarded-For"] = "198.51.100.1"
    event["multiValueHeaders"]["X-Forwarded-For"] = ["198.51.100.1"]

    invoke(event)

    assert set(rate_limits.counters.counts) == {
        f"ip#203.0.113.7#{WINDOW}",
        f"user#alice-sub#{WINDOW}",
    }


def test_ipv6_clients_share_their_64(rate_limits: RateLimitsInUse) -> None:
    for address in ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9"):
        event = get_order_event(ORDER.id)
        event["requestContext"]["identity"]["sourceIp"] = address
        invoke(event)

    assert rate_limits.counters.counts[f"ip#2001:db8:1:2::/64#{WINDOW}"] == 2


def test_a_request_without_a_source_ip_fails_closed(
    rate_limits: RateLimitsInUse, log_output: io.StringIO
) -> None:
    event = get_order_event(ORDER.id)
    del event["requestContext"]["identity"]["sourceIp"]

    response = invoke(event)

    problem_of(response, 500, "internal-error")
    assert rate_limits.counters.counts == {}
    assert '"source_ip_missing"' in log_output.getvalue()


def test_while_the_counter_store_is_unavailable_requests_are_counted_in_memory(
    rate_limits: RateLimitsInUse,
) -> None:
    rate_limits.use_counters(UnreliableRateLimitCounters(CounterStoreUnavailable("Timeout")))
    rate_limits.config.limits = TINY

    responses = [invoke(get_order_event(ORDER.id)) for _ in range(3)]

    assert [response["statusCode"] for response in responses] == [200, 200, 429]
    assert rate_limit_headers(responses[0])["RateLimit-Remaining"] == "1"
    assert problem_of(responses[-1], 429, "rate-limited")["scope"] == "user"


def test_a_throttled_counter_is_a_429(rate_limits: RateLimitsInUse) -> None:
    rate_limits.use_counters(
        UnreliableRateLimitCounters(CounterStoreThrottled("ThrottlingException"))
    )

    response = invoke(get_order_event(ORDER.id))

    assert problem_of(response, 429, "rate-limited")["scope"] == "ip"
    assert rate_limit_headers(response)["RateLimit-Remaining"] == "0"


def operational_lines(log_output: io.StringIO, event: str) -> list[dict[str, Any]]:
    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    return [line for line in lines if line["message"] == event]


def test_an_unavailable_counter_store_is_logged_as_a_warning_without_the_client_address(
    rate_limits: RateLimitsInUse, log_output: io.StringIO
) -> None:
    rate_limits.use_counters(
        UnreliableRateLimitCounters(CounterStoreUnavailable("ReadTimeoutError"))
    )

    invoke(get_order_event(ORDER.id))

    [line] = operational_lines(log_output, "rate_limiter_unavailable")
    assert line["level"] == "WARNING"
    assert line["errorType"] == "ReadTimeoutError"
    assert line["consecutiveFailures"] == 1
    assert line["requestId"]
    assert "203.0.113.7" not in log_output.getvalue()


def test_the_circuit_opening_and_closing_are_logged(
    rate_limits: RateLimitsInUse, log_output: io.StringIO
) -> None:
    counters = UnreliableRateLimitCounters(CounterStoreUnavailable("ReadTimeoutError"))
    rate_limits.use_counters(counters)
    for _ in range(FAILURES_TO_OPEN):
        invoke(get_order_event(ORDER.id))
        rate_limits.time.advance(REST_AFTER_FAILURE_SECONDS)
    counters.failure = None
    rate_limits.time.advance(OPEN_SECONDS)

    invoke(get_order_event(ORDER.id))

    [opened] = operational_lines(log_output, "rate_limiter_circuit_open")
    assert opened["level"] == "WARNING"
    assert opened["openSeconds"] == OPEN_SECONDS
    [closed] = operational_lines(log_output, "rate_limiter_circuit_closed")
    assert closed["level"] == "INFO"


def test_an_unexpected_counter_store_error_is_a_500_without_rate_limit_headers(
    rate_limits: RateLimitsInUse,
) -> None:
    """Only outages fail open; a defect (e.g. a missing permission) fails loudly."""
    rate_limits.use_counters(FailingRateLimitCounters())

    response = invoke(get_order_event(ORDER.id))

    problem_of(response, 500, "internal-error")
    assert rate_limit_headers(response) == {}


def access_line(log_output: io.StringIO) -> dict[str, Any]:
    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    line: dict[str, Any] = next(line for line in lines if line["message"] == "request")
    return line


def test_the_access_line_of_a_429_names_the_scope_and_only_the_client_s_network(
    rate_limits: RateLimitsInUse, log_output: io.StringIO
) -> None:
    rate_limits.config.limits = TINY_IP
    for _ in range(2):
        invoke(get_order_event(ORDER.id))
    log_output.truncate(0)
    log_output.seek(0)

    invoke(get_order_event(ORDER.id))

    line = access_line(log_output)
    assert line["status"] == 429
    assert line["rateLimitScope"] == "ip"
    assert line["clientNetwork"] == "203.0.113.0/24"
    assert "203.0.113.7" not in log_output.getvalue()


def test_an_allowed_request_logs_no_rate_limit_fields(
    rate_limits: RateLimitsInUse, log_output: io.StringIO
) -> None:
    invoke(get_order_event(ORDER.id))

    line = access_line(log_output)
    assert "rateLimitScope" not in line
    assert "clientNetwork" not in line
