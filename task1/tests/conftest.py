from collections.abc import Callable
from typing import Any

import pytest

from orders_api_infra.config import load_config
from orders_api_infra.demo_users import access_token, demo_user
from tests.deployment import load_endpoints, point_aws_sdk_at


@pytest.fixture(scope="session")
def endpoints() -> dict[str, Any]:
    """The deployed environment from build/endpoints.json, with boto3 pointed at it."""
    endpoints = load_endpoints()
    point_aws_sdk_at(endpoints)
    return endpoints


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
