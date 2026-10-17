"""`GET /api/v1/orders` through the deployed stack: walking every page (ADRs 0004 and 0011).

The table outlives test runs and other tests create orders too, so these tests check the orders
they created, plus properties of the whole walk (each order once, newest first), never counts.
Lists are eventually consistent, so a list checked right after a create is polled.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.requests import bearer
from tests.polling import eventually

ORDERS = "/api/v1/orders"
MAX_PAGES = 10_000  # a cursor that never ends would otherwise loop forever


@pytest.fixture
def list_as(api: httpx.Client, token_for: Callable[[str], str]) -> Callable[..., dict[str, Any]]:
    """One page for the given demo user, after checking it is a `200`."""

    def list_page(user: str, **query: str) -> dict[str, Any]:
        response = api.get(ORDERS, params=query, headers=bearer(token_for(user)))
        assert response.status_code == 200, response.text
        assert_contract_headers(response)
        page: dict[str, Any] = response.json()
        return page

    return list_page


@pytest.fixture
def walk_as(list_as: Callable[..., dict[str, Any]]) -> Callable[..., list[list[dict[str, Any]]]]:
    """Every page for the given demo user, following `nextCursor` until it is `null`."""

    def walk(user: str, limit: int, cursor: str | None = None) -> list[list[dict[str, Any]]]:
        pages = []
        for _ in range(MAX_PAGES):
            query = {"limit": str(limit)} | ({"cursor": cursor} if cursor else {})
            page = list_as(user, **query)
            pages.append(page["items"])
            cursor = page["nextCursor"]
            if cursor is None:
                return pages
        pytest.fail(f"no end after {MAX_PAGES} pages")

    return walk


def flat(pages: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [order for page in pages for order in page]


def ids(orders: list[dict[str, Any]]) -> list[str]:
    return [order["id"] for order in orders]


def newest_first(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(orders, key=lambda o: (o["createdAt"], o["id"]), reverse=True)


def assert_each_once_newest_first(orders: list[dict[str, Any]]) -> None:
    keys = [(order["createdAt"], order["id"]) for order in orders]
    assert keys == sorted(set(keys), reverse=True)


def created(create_order_as: Callable[[str], dict[str, Any]], *users: str) -> list[dict[str, Any]]:
    """One new order per user, in turn; returned newest first."""
    return newest_first([create_order_as(user) for user in users])


def walk_showing(
    walk_as: Callable[..., list[list[dict[str, Any]]]], user: str, orders: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The user's whole list, once it shows all of `orders`, newest first. Where they sit in it
    isn't assumed: orders written straight to the table may carry any `createdAt`."""

    def walked() -> list[dict[str, Any]]:
        walk = flat(walk_as(user, limit=100))
        assert [order for order in walk if order["id"] in ids(orders)] == orders
        return walk

    return eventually(walked)


def cursor_after(list_as: Callable[..., dict[str, Any]], user: str, order_id: str) -> str:
    """A cursor whose next page starts right after the given order."""
    query: dict[str, str] = {"limit": "100"}
    while True:
        page = list_as(user, **query)
        if order_id in ids(page["items"]):
            ending = list_as(user, **query | {"limit": str(ids(page["items"]).index(order_id) + 1)})
            assert ids(ending["items"])[-1] == order_id
            return next_cursor(ending)
        query = {"limit": "100", "cursor": next_cursor(page)}


def next_cursor(page: dict[str, Any]) -> str:
    cursor = page["nextCursor"]
    assert cursor is not None
    return str(cursor)


def test_alice_walks_all_her_pages_and_sees_each_of_her_orders_once_newest_first(
    walk_as: Callable[..., list[list[dict[str, Any]]]],
    create_order_as: Callable[[str], dict[str, Any]],
    subject_for: Callable[[str], str],
) -> None:
    mine = created(create_order_as, "alice", "alice", "alice")
    walk_showing(walk_as, "alice", mine)

    pages = walk_as("alice", limit=2)

    walked = flat(pages)
    assert len(pages) >= 2
    assert_each_once_newest_first(walked)
    assert [order for order in walked if order["id"] in ids(mine)] == mine
    assert {order["ownerId"] for order in walked} == {subject_for("alice")}


def test_the_manager_s_walk_includes_orders_of_several_owners(
    walk_as: Callable[..., list[list[dict[str, Any]]]],
    create_order_as: Callable[[str], dict[str, Any]],
    subject_for: Callable[[str], str],
) -> None:
    ours = created(create_order_as, "alice", "bob")

    walked = walk_showing(walk_as, "manager", ours)

    assert_each_once_newest_first(walked)
    assert {subject_for("alice"), subject_for("bob")} <= {order["ownerId"] for order in walked}


def test_orders_created_during_a_walk_cause_no_repeats_or_gaps(
    list_as: Callable[..., dict[str, Any]],
    walk_as: Callable[..., list[list[dict[str, Any]]]],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    """The walk is paused right after alice's newest order; a new order lands in front of it."""
    mine = created(create_order_as, "alice", "alice", "alice")
    before = ids(walk_showing(walk_as, "alice", mine))
    paused = cursor_after(list_as, "alice", mine[0]["id"])

    newer = created(create_order_as, "alice")
    walk_showing(walk_as, "alice", newer + mine)
    rest = ids(flat(walk_as("alice", limit=2, cursor=paused)))

    assert rest == before[before.index(mine[0]["id"]) + 1 :]
    assert newer[0]["id"] not in rest


def test_the_last_page_may_be_empty_and_the_walk_ends_on_a_null_cursor(
    list_as: Callable[..., dict[str, Any]],
    walk_as: Callable[..., list[list[dict[str, Any]]]],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    """A page that ends exactly at the oldest order still has a cursor; the page after it is
    empty. Asking for exactly as many orders as the last page held makes that happen."""
    walk_showing(walk_as, "alice", created(create_order_as, "alice"))
    query: dict[str, str] = {"limit": "100"}
    page = list_as("alice", **query)
    while page["nextCursor"] is not None:
        query = {"limit": "100", "cursor": page["nextCursor"]}
        page = list_as("alice", **query)

    if page["items"]:
        full = list_as("alice", **query | {"limit": str(len(page["items"]))})
        assert ids(full["items"]) == ids(page["items"])
        page = list_as("alice", limit="100", cursor=next_cursor(full))

    assert page == {"items": [], "nextCursor": None}


# Query parameters are checked by the API function: the gateway only checks presence.


@pytest.fixture
def alice(token_for: Callable[[str], str]) -> dict[str, str]:
    return bearer(token_for("alice"))


def assert_parameter_errors(response: httpx.Response, *expected: tuple[str, str]) -> None:
    assert_problem(response, 400, "validation-error")
    assert [(e["parameter"], e["detail"]) for e in response.json()["errors"]] == list(expected)


@pytest.mark.parametrize("name", ["status", "sort"])
def test_any_other_query_parameter_is_a_400(
    api: httpx.Client, alice: dict[str, str], name: str
) -> None:
    response = api.get(ORDERS, params={name: "x"}, headers=alice)

    assert_parameter_errors(response, (name, "is not a parameter of this operation"))


@pytest.mark.parametrize("limit", ["0", "101", "ten", "1.5"])
def test_a_limit_outside_1_to_100_is_a_400(
    api: httpx.Client, alice: dict[str, str], limit: str
) -> None:
    response = api.get(ORDERS, params={"limit": limit}, headers=alice)

    assert_parameter_errors(response, ("limit", "must be an integer from 1 to 100"))


def test_a_repeated_parameter_is_a_400(api: httpx.Client, alice: dict[str, str]) -> None:
    response = api.get(ORDERS, params=[("limit", "5"), ("limit", "6")], headers=alice)

    assert_parameter_errors(response, ("limit", "must be given only once"))


@pytest.mark.parametrize("cursor", ["not-a-cursor", "eyJvd25lciI6bnVsbH0", "%%%"])
def test_a_cursor_that_does_not_decode_is_a_400(
    api: httpx.Client, alice: dict[str, str], cursor: str
) -> None:
    response = api.get(ORDERS, params={"cursor": cursor}, headers=alice)

    assert_parameter_errors(response, ("cursor", "is not a valid cursor"))


@pytest.mark.parametrize(
    ("issued_to", "used_by"),
    [("alice", "bob"), ("alice", "manager"), ("manager", "alice")],
    ids=["alice-to-bob", "alice-to-manager", "manager-to-alice"],
)
def test_a_cursor_from_another_caller_s_list_is_a_400(
    api: httpx.Client,
    token_for: Callable[[str], str],
    list_as: Callable[..., dict[str, Any]],
    create_order_as: Callable[[str], dict[str, Any]],
    issued_to: str,
    used_by: str,
) -> None:
    created(create_order_as, "alice", "alice")
    cursor = eventually(lambda: next_cursor(list_as(issued_to, limit="1")))

    response = api.get(ORDERS, params={"cursor": cursor}, headers=bearer(token_for(used_by)))

    assert_parameter_errors(response, ("cursor", "belongs to a different list of orders"))
