from typing import Any

import pytest

from tests.deployment import load_endpoints, point_aws_sdk_at


@pytest.fixture(scope="session")
def endpoints() -> dict[str, Any]:
    """The deployed environment from build/endpoints.json, with boto3 pointed at it."""
    endpoints = load_endpoints()
    point_aws_sdk_at(endpoints)
    return endpoints
