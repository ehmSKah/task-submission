"""Status changes through the real path: every allowed and disallowed change, who may make it,
`If-Match`, and races (ADRs 0001, 0004 and 0011)."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.domain.order import Order, OrderStatus
from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.requests import bearer

PENDING, CONFIRMED, SHIPPED, DELIVERED, CANCELLED = OrderStatus
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"

ALLOWED = [
    (PENDING, CONFIRMED),
    (CONFIRMED, SHIPPED),
    (SHIPPED, DELIVERED),
    (PENDING, CANCELLED),
    (CONFIRMED, CANCELLED),
]
DISALLOWED = [
    (current, requested)
    for current in OrderStatus
    for requested in OrderStatus
    if current is not requested and (current, requested) not in ALLOWED
]


def status_path(order_id: str) -> str:
    return f"/api/v1/orders/{order_id}/status"


@pytest.fixture
def patch_status(
    api: httpx.Client, token_for: Callable[[str], str]
) -> Callable[..., httpx.Response]:
    def patch(
        user: str, order_id: str, status: OrderStatus, if_match: str | None = None
    ) -> httpx.Response:
        headers = bearer(token_for(user))
        if if_match is not None:
            headers["If-Match"] = if_match
        return api.patch(status_path(order_id), json={"status": status.value}, headers=headers)

    return patch


def read(api: httpx.Client, token_for: Callable[[str], str], order_id: str) -> httpx.Response:
    return api.get(f"/api/v1/orders/{order_id}", headers=bearer(token_for("admin")))


# Every allowed change, and who may make it.


@pytest.mark.parametrize("user", ["manager", "admin"])
@pytest.mark.parametrize(("current", "requested"), ALLOWED)
def test_staff_make_every_allowed_change(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
    user: str,
    current: OrderStatus,
    requested: OrderStatus,
) -> None:
    order = alices_order_in(current, version=2)

    response = patch_status(user, order.id, requested)

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["version"]) == (requested.value, 3)
    assert response.headers["ETag"] == '"3"'
    assert_contract_headers(response)
    assert read(api, token_for, order.id).json() == response.json()


@pytest.mark.parametrize("current", [PENDING, CONFIRMED])
def test_the_owner_cancels_their_own_order_before_it_ships(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    current: OrderStatus,
) -> None:
    order = alices_order_in(current)

    response = patch_status("alice", order.id, CANCELLED)

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


@pytest.mark.parametrize(("current", "requested"), [c for c in ALLOWED if c[1] is not CANCELLED])
def test_the_owner_may_make_no_other_change_even_where_it_is_allowed(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    current: OrderStatus,
    requested: OrderStatus,
) -> None:
    """Includes alice patching her own order to CONFIRMED."""
    order = alices_order_in(current)

    assert_problem(patch_status("alice", order.id, requested), 403, "forbidden")


@pytest.mark.parametrize("current", [PENDING, CONFIRMED])
def test_bob_cancelling_alice_s_order_is_a_404(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
    current: OrderStatus,
) -> None:
    order = alices_order_in(current)

    assert_problem(patch_status("bob", order.id, CANCELLED), 404, "not-found")
    assert read(api, token_for, order.id).json()["status"] == current.value


def test_an_order_created_through_the_api_walks_the_whole_lifecycle(
    patch_status: Callable[..., httpx.Response],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    order = create_order_as("alice")

    for version, status in enumerate([CONFIRMED, SHIPPED, DELIVERED], start=2):
        response = patch_status("manager", order["id"], status)

        assert response.status_code == 200
        assert response.json() == {
            **order,
            "status": status.value,
            "version": version,
            "updatedAt": response.json()["updatedAt"],
        }
        assert response.headers["ETag"] == f'"{version}"'


# Every disallowed change is a 409, whoever asks.


@pytest.mark.parametrize(("current", "requested"), DISALLOWED)
def test_every_disallowed_change_is_a_409(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
    current: OrderStatus,
    requested: OrderStatus,
) -> None:
    order = alices_order_in(current, version=3)

    response = patch_status("manager", order.id, requested)

    assert_problem(response, 409, "conflict")
    assert response.json()["detail"] == f"An order can't change from {current} to {requested}"
    assert read(api, token_for, order.id).json() == order_to_json(order)


@pytest.mark.parametrize("current", [SHIPPED, DELIVERED])
def test_the_owner_cancelling_after_shipping_is_a_409(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    current: OrderStatus,
) -> None:
    order = alices_order_in(current)

    assert_problem(patch_status("alice", order.id, CANCELLED), 409, "conflict")


# The same status again changes nothing.


@pytest.mark.parametrize("status", list(OrderStatus))
def test_the_same_status_again_is_a_200_with_the_version_unchanged(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    status: OrderStatus,
) -> None:
    order = alices_order_in(status, version=4)

    response = patch_status("manager", order.id, status)

    assert response.status_code == 200
    assert response.json() == order_to_json(order)
    assert response.headers["ETag"] == '"4"'


def test_the_owner_cancelling_a_cancelled_order_again_is_a_200(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
) -> None:
    order = alices_order_in(CANCELLED, version=2)

    response = patch_status("alice", order.id, CANCELLED)

    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_an_unknown_order_is_a_404(patch_status: Callable[..., httpx.Response]) -> None:
    assert_problem(patch_status("manager", UNKNOWN_ID, CONFIRMED), 404, "not-found")


# If-Match: the ETag from a GET, compared before anything else about the order's state.


def test_if_match_with_the_current_etag_changes_the_order_and_a_stale_one_is_a_412(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
) -> None:
    order = alices_order_in(PENDING)
    etag = read(api, token_for, order.id).headers["ETag"]

    confirmed = patch_status("manager", order.id, CONFIRMED, if_match=etag)
    stale = patch_status("manager", order.id, SHIPPED, if_match=etag)
    fresh = patch_status("manager", order.id, SHIPPED, if_match=confirmed.headers["ETag"])

    assert confirmed.status_code == 200
    assert_problem(stale, 412, "precondition-failed")
    assert fresh.status_code == 200
    assert fresh.headers["ETag"] == '"3"'


@pytest.mark.parametrize(
    "requested", [CONFIRMED, PENDING, DELIVERED], ids=["allowed", "same-status", "not-allowed"]
)
def test_a_stale_if_match_is_a_412_whatever_was_requested(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
    requested: OrderStatus,
) -> None:
    order = alices_order_in(PENDING, version=2)

    assert_problem(
        patch_status("manager", order.id, requested, if_match='"1"'), 412, "precondition-failed"
    )
    assert read(api, token_for, order.id).json() == order_to_json(order)


def test_if_match_star_accepts_any_version(
    patch_status: Callable[..., httpx.Response], alices_order_in: Callable[..., Order]
) -> None:
    order = alices_order_in(PENDING, version=5)

    assert patch_status("manager", order.id, CONFIRMED, if_match="*").status_code == 200


# Races: a simultaneous ship and cancel yield exactly one 200 and one 409 (ADR 0011).


@pytest.mark.parametrize("attempt", range(5))
def test_a_simultaneous_ship_and_cancel_yield_exactly_one_200_and_one_409(
    patch_status: Callable[..., httpx.Response],
    alices_order_in: Callable[..., Order],
    api: httpx.Client,
    token_for: Callable[[str], str],
    attempt: int,
) -> None:
    order = alices_order_in(CONFIRMED, version=2)
    racers = [("manager", SHIPPED), ("alice", CANCELLED)]
    start = threading.Barrier(len(racers))

    def race(racer: tuple[str, OrderStatus]) -> httpx.Response:
        user, status = racer
        start.wait()
        return patch_status(user, order.id, status)

    with ThreadPoolExecutor(max_workers=len(racers)) as pool:
        shipped, cancelled = pool.map(race, racers)

    assert sorted([shipped.status_code, cancelled.status_code]) == [200, 409]
    winner, loser = (shipped, cancelled) if shipped.status_code == 200 else (cancelled, shipped)
    assert_problem(loser, 409, "conflict")
    assert winner.json()["version"] == 3
    final = read(api, token_for, order.id).json()
    assert final == winner.json()
    assert loser.json()["detail"].startswith(f"An order can't change from {final['status']}")
