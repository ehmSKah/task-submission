"""Successful requests per role through the real path, with the seeded demo users (ADR 0001)."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tests.integration.assertions import assert_contract_headers
from tests.integration.requests import bearer

CREATORS = ["alice", "bob", "manager", "admin"]


@pytest.mark.parametrize("user", CREATORS)
def test_every_role_creates_an_order_it_owns_and_reads_it_back(
    api: httpx.Client,
    token_for: Callable[[str], str],
    subject_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    user: str,
) -> None:
    order = create_order_as(user)

    assert order["ownerId"] == subject_for(user)

    read = api.get(f"/api/v1/orders/{order['id']}", headers=bearer(token_for(user)))

    assert read.status_code == 200
    assert read.json() == order
    assert_contract_headers(read)


@pytest.mark.parametrize("staff", ["manager", "admin"])
def test_staff_read_alice_s_order(
    api: httpx.Client,
    token_for: Callable[[str], str],
    subject_for: Callable[[str], str],
    create_order_as: Callable[[str], dict[str, Any]],
    staff: str,
) -> None:
    alices = create_order_as("alice")

    response = api.get(f"/api/v1/orders/{alices['id']}", headers=bearer(token_for(staff)))

    assert response.status_code == 200
    assert response.json() == alices
    assert response.json()["ownerId"] == subject_for("alice")
    assert response.headers["ETag"] == '"1"'
