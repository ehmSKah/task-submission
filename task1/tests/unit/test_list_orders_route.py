"""`GET /api/v1/orders`: newest first, cursor pagination, strict query parameters (ADR 0004)."""

import json
from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.domain.order import Order
from tests.builders import an_order
from tests.unit.events import (
    ADMIN,
    ALICE,
    BOB,
    MANAGER,
    headers_of,
    invoke,
    list_orders_event,
    problem_of,
)
from tests.unit.fakes import InMemoryOrderRepository, WireApi


def order(n: int, owner: str = "alice-sub") -> Order:
    return an_order(
        id=f"00000000-0000-4000-8000-{n:012d}",
        owner_id=owner,
        created_at=f"2026-10-17T09:{n // 60:02d}:{n % 60:02d}.000Z",
    )


class UnreadableRepository(InMemoryOrderRepository):
    def list_newest_first(self, **_: Any) -> Any:
        raise AssertionError("the repository must not be read")


def listed(query: dict[str, Any] | None = None, caller: dict[str, str] = ALICE) -> dict[str, Any]:
    response = invoke(list_orders_event(query, caller))
    assert response["statusCode"] == 200, response["body"]
    assert headers_of(response)["Content-Type"] == "application/json"
    body: dict[str, Any] = json.loads(response["body"])
    return body


def walk(caller: dict[str, str], limit: int) -> list[list[dict[str, Any]]]:
    pages, query = [], {"limit": str(limit)}
    while True:
        page = listed(query, caller)
        pages.append(page["items"])
        if page["nextCursor"] is None:
            return pages
        query = {"limit": str(limit), "cursor": page["nextCursor"]}


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["id"] for item in items]


def test_a_user_gets_their_own_orders_newest_first_in_the_published_shape(
    wire_api: WireApi,
) -> None:
    wire_api([order(1), order(2, "bob-sub"), order(3)])

    page = listed()

    assert page == {"items": [order_to_json(order(3)), order_to_json(order(1))], "nextCursor": None}
    assert page["items"][0] == {
        "id": order(3).id,
        "ownerId": "alice-sub",
        "status": "PENDING",
        "currency": "EUR",
        "items": [{"sku": "ABC-123", "quantity": 2, "unitPrice": 1999}],
        "totalAmount": 3998,
        "createdAt": "2026-10-17T09:00:03.000Z",
        "updatedAt": "2026-10-17T09:30:00.123Z",
        "version": 1,
    }


def test_an_empty_list_is_an_empty_page(wire_api: WireApi) -> None:
    wire_api([order(1, "bob-sub")])

    assert listed() == {"items": [], "nextCursor": None}


@pytest.mark.parametrize("staff", [MANAGER, ADMIN], ids=["manager", "admin"])
def test_staff_with_read_any_get_everyone_s_orders(
    wire_api: WireApi, staff: dict[str, str]
) -> None:
    wire_api([order(1), order(2, "bob-sub")])

    assert ids(listed(caller=staff)["items"]) == [order(2).id, order(1).id]


def test_the_default_page_holds_ten_orders(wire_api: WireApi) -> None:
    wire_api([order(n) for n in range(1, 13)])

    page = listed()

    assert ids(page["items"]) == [order(n).id for n in range(12, 2, -1)]
    assert page["nextCursor"]


def test_a_page_holds_up_to_a_hundred_orders(wire_api: WireApi) -> None:
    wire_api([order(n) for n in range(1, 102)])

    assert len(listed({"limit": "100"})["items"]) == 100


def test_following_next_cursor_walks_every_order_once_newest_first(wire_api: WireApi) -> None:
    wire_api([order(n) for n in range(1, 8)] + [order(8, "bob-sub")])

    pages = walk(ALICE, limit=3)

    assert [len(page) for page in pages] == [3, 3, 1]
    assert ids([item for page in pages for item in page]) == [order(n).id for n in range(7, 0, -1)]


def test_the_last_page_may_be_empty_and_has_no_next_cursor(wire_api: WireApi) -> None:
    wire_api([order(n) for n in range(1, 5)])

    assert [len(page) for page in walk(ALICE, limit=2)] == [2, 2, 0]


# Query parameters are checked before any data access (ADR 0007).


@pytest.fixture
def unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_handler, "_orders", UnreadableRepository())


def errors_of(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [(error["parameter"], error["detail"]) for error in body["errors"]]


@pytest.mark.usefixtures("unreadable")
@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc", "1.5", "1e2", "", " 5", "٣"])
def test_a_limit_outside_1_to_100_is_a_400_naming_the_parameter(limit: str) -> None:
    body = problem_of(invoke(list_orders_event({"limit": limit})), 400, "validation-error")

    assert body["detail"] == "Invalid query parameter: limit"
    assert errors_of(body) == [("limit", "must be an integer from 1 to 100")]


@pytest.mark.usefixtures("unreadable")
@pytest.mark.parametrize("name", ["status", "sort", "ownerId", "Limit"])
def test_any_other_query_parameter_is_a_400(name: str) -> None:
    body = problem_of(invoke(list_orders_event({name: "x"})), 400, "validation-error")

    assert errors_of(body) == [(name, "is not a parameter of this operation")]


@pytest.mark.usefixtures("unreadable")
def test_a_repeated_parameter_is_a_400() -> None:
    body = problem_of(invoke(list_orders_event({"limit": ["5", "6"]})), 400, "validation-error")

    assert errors_of(body) == [("limit", "must be given only once")]


@pytest.mark.usefixtures("unreadable")
def test_every_offending_parameter_is_reported_at_once() -> None:
    event = list_orders_event({"sort": "createdAt", "limit": "0", "cursor": "nope"})

    body = problem_of(invoke(event), 400, "validation-error")

    assert body["detail"] == "Invalid query parameters: cursor, limit, sort"
    assert [parameter for parameter, _ in errors_of(body)] == ["cursor", "limit", "sort"]


@pytest.mark.usefixtures("unreadable")
def test_a_cursor_that_does_not_decode_is_a_400() -> None:
    body = problem_of(invoke(list_orders_event({"cursor": "bm90IGEgY3Vyc29y"})), 400)

    assert errors_of(body) == [("cursor", "is not a valid cursor")]


@pytest.mark.parametrize(
    ("issued_to", "used_by"),
    [(ALICE, BOB), (ALICE, MANAGER), (MANAGER, ALICE)],
    ids=["alice-to-bob", "alice-to-manager", "manager-to-alice"],
)
def test_a_cursor_from_another_caller_s_list_is_a_400(
    wire_api: WireApi, issued_to: dict[str, str], used_by: dict[str, str]
) -> None:
    wire_api([order(1), order(2), order(3, "bob-sub")])
    cursor = listed({"limit": "1"}, issued_to)["nextCursor"]

    body = problem_of(invoke(list_orders_event({"cursor": cursor}, used_by)), 400)

    assert errors_of(body) == [("cursor", "belongs to a different list of orders")]


def test_a_cursor_resumes_for_anyone_with_the_same_list(wire_api: WireApi) -> None:
    """The cursor is bound to the list, not the person: all staff share every owner's list."""
    wire_api([order(1), order(2, "bob-sub")])
    cursor = listed({"limit": "1"}, MANAGER)["nextCursor"]

    assert ids(listed({"cursor": cursor}, ADMIN)["items"]) == [order(1).id]
