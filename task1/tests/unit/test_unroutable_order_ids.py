"""An order id the function's routes can't match is still a malformed id: `400`, like any other
id that isn't a UUID (ADRs 0007 and 0009).

The gateway matches `{id}` to any one path segment, but the router only to word characters and
the URI-safe ones, so an id holding e.g. a control character (`%C2%84` decodes to U+0084) would
otherwise fall through to "no such route", a `404`.
"""

from collections.abc import Callable
from typing import Any

import pytest

from tests.unit.events import (
    ADMIN,
    MANAGER,
    NOBODY,
    change_status_event,
    delete_order_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import WireApi

UNROUTABLE_IDS = ["\u0084", "abc​def", "\x00"]

Event = Callable[[str, dict[str, str]], dict[str, Any]]
OPERATIONS: dict[str, Event] = {
    "get": lambda order_id, caller: get_order_event(order_id, caller),
    "delete": lambda order_id, caller: delete_order_event(order_id, caller),
    "change-status": lambda order_id, caller: change_status_event(order_id, authorizer=caller),
}


@pytest.fixture(autouse=True)
def no_orders(wire_api: WireApi) -> None:
    wire_api([])


@pytest.mark.parametrize("order_id", UNROUTABLE_IDS, ids=repr)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_an_id_the_routes_can_t_match_is_a_400_naming_the_parameter(
    operation: str, order_id: str
) -> None:
    caller = MANAGER if operation == "change-status" else ADMIN

    body = problem_of(invoke(OPERATIONS[operation](order_id, caller)), 400, "validation-error")

    assert [error["parameter"] for error in body["errors"]] == ["id"]


@pytest.mark.parametrize("operation", OPERATIONS)
def test_it_is_a_400_even_for_nobody_and_counts_against_the_rate_limits(operation: str) -> None:
    """Steps 2 to 4 as for any other request: identity, the rate limits, then the request
    checks, before the permission check could answer `403`."""
    response = invoke(OPERATIONS[operation]("\u0084", NOBODY))

    problem_of(response, 400, "validation-error")
    assert "RateLimit-Limit" in headers_of(response)
