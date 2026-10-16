from collections.abc import Callable

import httpx

from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.domain.order import Order
from tests.integration.assertions import assert_contract_headers, assert_problem


def test_signed_in_user_reads_an_order(
    api: httpx.Client, token_for: Callable[[str], str], stored_order: Order
) -> None:
    response = api.get(
        f"/api/v1/orders/{stored_order.id}",
        headers={"Authorization": f"Bearer {token_for('alice')}"},
    )

    assert response.status_code == 200
    assert response.json() == order_to_json(stored_order)
    assert_contract_headers(response)


def test_unknown_order_is_a_problem_404(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    response = api.get(
        "/api/v1/orders/0e6bb2c6-0000-4000-8000-000000000000",
        headers={"Authorization": f"Bearer {token_for('alice')}"},
    )

    assert_problem(response, 404, "not-found")
