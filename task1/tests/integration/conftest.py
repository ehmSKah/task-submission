"""Integration tests run over HTTP against the deployed stack named in build/endpoints.json."""

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from orders_api.domain.order import Order, OrderStatus, format_timestamp
from orders_api_infra.config import load_config
from orders_api_infra.demo_users import access_token, demo_user
from tests.builders import an_order
from tests.deployment import store_order
from tests.integration.contract import contract_checked_client
from tests.integration.requests import NEW_ORDER, bearer, subject_of


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def _only_on_floci(request: pytest.FixtureRequest, endpoints: dict[str, Any]) -> None:
    """`floci_only` tests need emulator behaviour, e.g. second-long tokens (ADR 0011)."""
    if request.node.get_closest_marker("floci_only") and endpoints["target"] != "floci":
        pytest.skip("runs against Floci only")


@pytest.fixture(scope="session")
def api(endpoints: dict[str, Any]) -> Iterator[httpx.Client]:
    """Every response through this client is validated against the OpenAPI document."""
    with contract_checked_client(endpoints["apiUrl"], timeout=30) as client:
        yield client


@pytest.fixture(scope="session")
def token_for(endpoints: dict[str, Any]) -> Callable[[str], str]:
    """A real access token from the environment's Cognito, per demo user, fetched once."""
    config = load_config(endpoints["env"])
    tokens: dict[str, str] = {}

    def token(user: str) -> str:
        if user not in tokens:
            tokens[user] = access_token(config, demo_user(user))
        return tokens[user]

    return token


@pytest.fixture(scope="session")
def subject_for(token_for: Callable[[str], str]) -> Callable[[str], str]:
    """A demo user's `sub`, read from their token; the authorizer verifies it, tests only read."""

    def subject(user: str) -> str:
        return subject_of(token_for(user))

    return subject


@pytest.fixture
def stored_order(endpoints: dict[str, Any], subject_for: Callable[[str], str]) -> Order:
    """An order of alice's, written straight to the table, bypassing the API."""
    order = an_order(id=str(uuid.uuid4()), owner_id=subject_for("alice"))
    store_order(endpoints, order)
    return order


@pytest.fixture
def alices_order_in(
    endpoints: dict[str, Any], subject_for: Callable[[str], str]
) -> Callable[..., Order]:
    """Writes an order of alice's in the given status straight to the table and returns it.

    It is created now, so it never looks newer than orders the API creates later.
    """

    def store(status: OrderStatus, version: int = 1) -> Order:
        now = format_timestamp(datetime.now(UTC))
        order = an_order(
            id=str(uuid.uuid4()),
            owner_id=subject_for("alice"),
            status=status,
            version=version,
            created_at=now,
            updated_at=now,
        )
        store_order(endpoints, order)
        return order

    return store


@pytest.fixture
def create_order_as(
    api: httpx.Client, token_for: Callable[[str], str]
) -> Callable[[str], dict[str, Any]]:
    """Creates an order through the API as the given demo user and returns it."""

    def create(user: str) -> dict[str, Any]:
        response = api.post("/api/v1/orders", json=NEW_ORDER, headers=bearer(token_for(user)))
        assert response.status_code == 201, response.text
        order: dict[str, Any] = response.json()
        return order

    return create
