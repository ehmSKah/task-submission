"""A signed-in user creates an order through the real path (ADR 0004)."""

from collections.abc import Callable
from typing import Any

import httpx
import jwt

from tests.integration.assertions import assert_contract_headers
from tests.integration.requests import NEW_ORDER, bearer


def subject_of(token: str) -> str:
    """The token's `sub`; the authorizer has verified it, the test only reads it."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return str(claims["sub"])


def test_alice_creates_an_order_and_reads_it_back(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    token = token_for("alice")

    created = api.post("/api/v1/orders", json=NEW_ORDER, headers=bearer(token))

    assert created.status_code == 201
    order = created.json()
    assert order["totalAmount"] == 2 * 1999 + 3 * 250
    assert order["status"] == "PENDING"
    assert order["ownerId"] == subject_of(token)
    assert order["version"] == 1
    assert order["currency"] == "EUR"
    assert order["items"] == NEW_ORDER["items"]
    assert order["createdAt"] == order["updatedAt"]
    assert created.headers["ETag"] == '"1"'
    assert_contract_headers(created)

    read = api.get(f"/api/v1/orders/{order['id']}", headers=bearer(token))

    assert read.status_code == 200
    assert read.json() == order
    assert read.headers["ETag"] == '"1"'


def test_each_order_gets_its_own_id(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    headers = bearer(token_for("alice"))

    first = api.post("/api/v1/orders", json=NEW_ORDER, headers=headers).json()
    second = api.post("/api/v1/orders", json=NEW_ORDER, headers=headers).json()

    assert first["id"] != second["id"]
