"""Integration tests run over HTTP against the deployed stack named in build/endpoints.json."""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from orders_api.domain.order import Order
from orders_api_infra.config import load_config
from orders_api_infra.demo_users import access_token, demo_user
from tests.builders import an_order
from tests.deployment import store_order


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def api(endpoints: dict[str, Any]) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=endpoints["apiUrl"], timeout=30) as client:
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


@pytest.fixture
def stored_order(endpoints: dict[str, Any]) -> Order:
    """An order written straight to the table, until the API can create orders itself."""
    order = an_order(id=str(uuid.uuid4()))
    store_order(endpoints, order)
    return order
