"""Authorization failures through the real path, with the seeded demo users (ADRs 0001, 0011).

nobody holds a valid token but no recognised role; bob is a USER who doesn't own alice's order.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.assertions import assert_problem
from tests.integration.requests import NEW_ORDER, bearer

UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"


def without_per_request_fields(problem: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in problem.items() if k not in {"instance", "requestId"}}


def test_bob_gets_the_same_404_for_alice_s_order_as_for_an_unknown_one(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    alices = create_order_as("alice")
    headers = bearer(token_for("bob"))

    foreign = api.get(f"/api/v1/orders/{alices['id']}", headers=headers)
    unknown = api.get(f"/api/v1/orders/{UNKNOWN_ID}", headers=headers)

    assert_problem(foreign, 404, "not-found")
    assert_problem(unknown, 404, "not-found")
    assert without_per_request_fields(foreign.json()) == without_per_request_fields(unknown.json())


def test_nobody_may_not_create_an_order(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    response = api.post("/api/v1/orders", json=NEW_ORDER, headers=bearer(token_for("nobody")))

    assert_problem(response, 403, "forbidden")


@pytest.mark.parametrize("existing", [True, False], ids=["alice-s-order", "unknown-id"])
def test_nobody_may_not_read_an_order_whether_or_not_it_exists(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    existing: bool,
) -> None:
    order_id = create_order_as("alice")["id"] if existing else UNKNOWN_ID

    response = api.get(f"/api/v1/orders/{order_id}", headers=bearer(token_for("nobody")))

    assert_problem(response, 403, "forbidden")


def test_an_invalid_body_from_nobody_is_a_400_not_a_403(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    response = api.post(
        "/api/v1/orders",
        json={**NEW_ORDER, "currency": "XXX"},
        headers=bearer(token_for("nobody")),
    )

    assert_problem(response, 400, "validation-error")


def test_a_malformed_order_id_from_nobody_is_a_400_not_a_403(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    response = api.get("/api/v1/orders/not-a-uuid", headers=bearer(token_for("nobody")))

    assert_problem(response, 400, "validation-error")


@pytest.mark.parametrize("status", ["CANCELLED", "CONFIRMED"])
@pytest.mark.parametrize("existing", [True, False], ids=["alice-s-order", "unknown-id"])
def test_nobody_may_not_change_a_status_whether_or_not_the_order_exists(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    existing: bool,
    status: str,
) -> None:
    order_id = create_order_as("alice")["id"] if existing else UNKNOWN_ID

    response = api.patch(
        f"/api/v1/orders/{order_id}/status",
        json={"status": status},
        headers=bearer(token_for("nobody")),
    )

    assert_problem(response, 403, "forbidden")


@pytest.mark.parametrize("existing", [True, False], ids=["alice-s-order", "unknown-id"])
def test_nobody_may_not_delete_an_order_whether_or_not_it_exists(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    existing: bool,
) -> None:
    order_id = create_order_as("alice")["id"] if existing else UNKNOWN_ID

    response = api.delete(f"/api/v1/orders/{order_id}", headers=bearer(token_for("nobody")))

    assert_problem(response, 403, "forbidden")


def test_alice_patching_her_own_order_to_confirmed_is_a_403(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    order = create_order_as("alice")

    response = api.patch(
        f"/api/v1/orders/{order['id']}/status",
        json={"status": "CONFIRMED"},
        headers=bearer(token_for("alice")),
    )

    assert_problem(response, 403, "forbidden")


def test_bob_gets_the_same_404_cancelling_alice_s_order_as_an_unknown_one(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    alices = create_order_as("alice")
    headers = bearer(token_for("bob"))
    body = {"status": "CANCELLED"}

    foreign = api.patch(f"/api/v1/orders/{alices['id']}/status", json=body, headers=headers)
    unknown = api.patch(f"/api/v1/orders/{UNKNOWN_ID}/status", json=body, headers=headers)

    assert_problem(foreign, 404, "not-found")
    assert_problem(unknown, 404, "not-found")
    assert without_per_request_fields(foreign.json()) == without_per_request_fields(unknown.json())


def test_an_invalid_status_from_nobody_is_a_400_not_a_403(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    response = api.patch(
        f"/api/v1/orders/{UNKNOWN_ID}/status",
        json={"status": "LOST"},
        headers=bearer(token_for("nobody")),
    )

    assert_problem(response, 400, "validation-error")
