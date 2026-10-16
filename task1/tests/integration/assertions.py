"""Shared checks for the response contract every layer must honour (ADRs 0007 and 0012)."""

import httpx

from orders_api.adapters.inbound.http_headers import PROBLEM_JSON_MEDIA_TYPE, SECURITY_HEADERS


def assert_contract_headers(response: httpx.Response) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name
    assert response.headers.get("X-Request-Id")


def assert_problem(response: httpx.Response, status: int, slug: str) -> None:
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    body = response.json()
    assert body["type"] == f"urn:problem:orders-api:{slug}"
    assert body["requestId"] == response.headers["X-Request-Id"]
    assert_contract_headers(response)
