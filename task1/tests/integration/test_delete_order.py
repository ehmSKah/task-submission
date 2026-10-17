"""Deletion through the real path: an ADMIN-only hard delete in any status (ADRs 0001 and 0004)."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from orders_api.domain.order import Order, OrderStatus
from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.requests import bearer


def order_path(order_id: str) -> str:
    return f"/api/v1/orders/{order_id}"


def test_the_admin_deletes_an_order_with_204_and_it_is_a_404_afterwards(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
) -> None:
    order = create_order_as("alice")
    admin = bearer(token_for("admin"))

    deleted = api.delete(order_path(order["id"]), headers=admin)

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert_contract_headers(deleted)
    assert_problem(api.get(order_path(order["id"]), headers=admin), 404, "not-found")
    owner = bearer(token_for("alice"))
    assert_problem(api.get(order_path(order["id"]), headers=owner), 404, "not-found")
    assert_problem(api.delete(order_path(order["id"]), headers=admin), 404, "not-found")


@pytest.mark.parametrize("status", list(OrderStatus))
def test_an_order_is_deleted_in_any_status(
    api: httpx.Client,
    token_for: Callable[[str], str],
    alices_order_in: Callable[..., Order],
    status: OrderStatus,
) -> None:
    order = alices_order_in(status)

    response = api.delete(order_path(order.id), headers=bearer(token_for("admin")))

    assert response.status_code == 204


@pytest.mark.parametrize("user", ["alice", "manager"])
def test_the_owner_and_the_manager_may_not_delete(
    api: httpx.Client,
    token_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    user: str,
) -> None:
    order = create_order_as("alice")

    response = api.delete(order_path(order["id"]), headers=bearer(token_for(user)))

    assert_problem(response, 403, "forbidden")
    assert api.get(order_path(order["id"]), headers=bearer(token_for("alice"))).json() == order


def test_an_unknown_order_is_a_404(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    response = api.delete(
        order_path("0e6bb2c6-0000-4000-8000-000000000000"), headers=bearer(token_for("admin"))
    )

    assert_problem(response, 404, "not-found")


def test_a_malformed_id_is_a_400_naming_the_parameter(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    response = api.delete(order_path("not-a-uuid"), headers=bearer(token_for("admin")))

    assert_problem(response, 400, "validation-error")
    assert [error["parameter"] for error in response.json()["errors"]] == ["id"]
