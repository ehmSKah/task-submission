"""Every rejected token gets the same 401 from the gateway (ADR 0003)."""

import httpx
import pytest

from tests.integration.assertions import assert_problem

ORDER_PATH = "/api/v1/orders/0e6bb2c6-0000-4000-8000-000000000000"


def assert_unauthorized(response: httpx.Response) -> None:
    assert_problem(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_missing_token_is_rejected_by_the_gateway(api: httpx.Client) -> None:
    assert_unauthorized(api.get(ORDER_PATH))


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer a.b.c", "Basic YWxpY2U6cHc="])
def test_malformed_token_is_rejected(api: httpx.Client, header: str) -> None:
    assert_unauthorized(api.get(ORDER_PATH, headers={"Authorization": header}))
