"""Every applicable row of ADR 0007's edge-case table, through the deployed stack.

Shape errors are rejected by the gateway's request validator before the API function runs;
`Content-Type`, size, duplicate SKUs and the path id are checked by the function.
"""

import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from orders_api.adapters.inbound.request_checks import MAX_BODY_BYTES
from tests.integration.assertions import assert_problem
from tests.integration.function_logs import FunctionLogs, api_function_name
from tests.integration.requests import NEW_ORDER, bearer

ORDERS = "/api/v1/orders"
ITEM = NEW_ORDER["items"][0]


@pytest.fixture
def alice(token_for: Callable[[str], str]) -> dict[str, str]:
    return bearer(token_for("alice"))


def post_json(api: httpx.Client, headers: dict[str, str], body: Any) -> httpx.Response:
    return api.post(ORDERS, json=body, headers=headers)


def post_raw(
    api: httpx.Client, headers: dict[str, str], content: str, content_type: str = "application/json"
) -> httpx.Response:
    return api.post(ORDERS, content=content, headers={**headers, "Content-Type": content_type})


def assert_rejected_by_the_gateway(response: httpx.Response) -> None:
    """Gateway validation reports one message in `detail` and never lists `errors[]`."""
    assert_problem(response, 400, "validation-error")
    body = response.json()
    assert isinstance(body["detail"], str)
    assert "errors" not in body


@pytest.mark.parametrize(
    "body",
    [
        {"currency": "CHF", "items": [ITEM]},
        {"currency": "EUR", "items": []},
        {"currency": "EUR", "items": [{**ITEM, "quantity": 1000}]},
        {"currency": "EUR", "items": [{**ITEM, "unitPrice": 19.99}]},
        {"currency": "EUR", "items": [{**ITEM, "sku": "lower-case"}]},
        {"items": [ITEM]},
    ],
    ids=["currency", "no-items", "quantity", "unit-price", "sku", "missing-currency"],
)
def test_schema_violation_is_400(api: httpx.Client, alice: dict[str, str], body: Any) -> None:
    assert_rejected_by_the_gateway(post_json(api, alice, body))


@pytest.mark.parametrize("field", ["totalAmount", "ownerId"])
def test_unknown_field_is_400(api: httpx.Client, alice: dict[str, str], field: str) -> None:
    assert_rejected_by_the_gateway(post_json(api, alice, {**NEW_ORDER, field: 1}))


def test_schema_violation_detail_carries_the_validator_message(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    response = post_json(api, alice, {"currency": "EUR", "items": [{**ITEM, "quantity": 1000}]})

    assert_rejected_by_the_gateway(response)
    assert response.json()["detail"]


@pytest.mark.parametrize("content", ['{"currency": "EUR",', "not json", ""])
def test_invalid_or_empty_json_is_400(
    api: httpx.Client, alice: dict[str, str], content: str
) -> None:
    assert_problem(post_raw(api, alice, content), 400, "validation-error")


def test_validator_message_with_quotes_still_renders_valid_json(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    """A client-chosen field name ends up inside the gateway's validation message."""
    response = post_raw(api, alice, json.dumps({**NEW_ORDER, 'say "hi" \\o/': 1}))

    assert_rejected_by_the_gateway(response)


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml"])
def test_wrong_content_type_is_415(
    api: httpx.Client, alice: dict[str, str], content_type: str
) -> None:
    response = post_raw(api, alice, json.dumps(NEW_ORDER), content_type)

    assert_problem(response, 415, "unsupported-media-type")


def test_missing_content_type_is_415(api: httpx.Client, alice: dict[str, str]) -> None:
    """The gateway validates such a body as JSON; the function still insists on the header."""
    response = api.post(ORDERS, content=json.dumps(NEW_ORDER), headers=alice)

    assert "Content-Type" not in response.request.headers
    assert_problem(response, 415, "unsupported-media-type")


def test_oversized_body_is_413(api: httpx.Client, alice: dict[str, str]) -> None:
    """Whitespace keeps the body valid, so it passes the gateway and reaches the size check."""
    content = json.dumps(NEW_ORDER)
    content += " " * (MAX_BODY_BYTES + 1 - len(content))

    assert_problem(post_raw(api, alice, content), 413, "payload-too-large")


def test_duplicate_skus_are_400_with_errors(api: httpx.Client, alice: dict[str, str]) -> None:
    response = post_json(api, alice, {"currency": "EUR", "items": [ITEM, {**ITEM, "quantity": 1}]})

    assert_problem(response, 400, "validation-error")
    assert response.json()["errors"] == [
        {"pointer": "/items/1/sku", "detail": "duplicates the SKU at /items/0/sku"}
    ]


@pytest.mark.parametrize("order_id", ["not-a-uuid", "0E6BB2C6-0000-4000-8000-000000000000", "123"])
def test_malformed_path_id_is_400_naming_the_parameter(
    api: httpx.Client, alice: dict[str, str], order_id: str
) -> None:
    response = api.get(f"{ORDERS}/{order_id}", headers=alice)

    assert_problem(response, 400, "validation-error")
    assert [error["parameter"] for error in response.json()["errors"]] == ["id"]


# Ids the function's router can't match (a control character, a zero-width space): the gateway
# passes them on decoded, and they are malformed like any other id that isn't a UUID.
@pytest.mark.parametrize("encoded_id", ["%C2%84", "abc%E2%80%8Bdef"])
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [("GET", "", None), ("DELETE", "", None), ("PATCH", "/status", {"status": "CANCELLED"})],
    ids=["get", "delete", "change-status"],
)
def test_an_id_the_router_can_t_match_is_400_too(
    api: httpx.Client,
    alice: dict[str, str],
    encoded_id: str,
    method: str,
    suffix: str,
    body: Any,
) -> None:
    response = api.request(method, f"{ORDERS}/{encoded_id}{suffix}", json=body, headers=alice)

    assert_problem(response, 400, "validation-error")
    assert [error["parameter"] for error in response.json()["errors"]] == ["id"]


def test_the_authorizer_runs_before_request_validation(api: httpx.Client) -> None:
    """An invalid body without a token is a 401, not a 400 (ADR 0007, to verify)."""
    response = post_json(api, {}, {**NEW_ORDER, "totalAmount": 1})

    assert_problem(response, 401, "unauthorized")


def status_path(order_id: str = "0e6bb2c6-0000-4000-8000-000000000000") -> str:
    return f"{ORDERS}/{order_id}/status"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "LOST"},
        {"status": "shipped"},
        {"status": 3},
        {},
        {"status": "SHIPPED", "version": 2},
    ],
    ids=["unknown-status", "lower-case", "not-a-string", "missing-status", "unknown-field"],
)
def test_an_invalid_status_body_is_400_from_the_gateway(
    api: httpx.Client, alice: dict[str, str], body: Any
) -> None:
    assert_rejected_by_the_gateway(api.patch(status_path(), json=body, headers=alice))


@pytest.mark.parametrize("content", ['{"status": ', ""])
def test_an_invalid_or_empty_status_body_is_400(
    api: httpx.Client, alice: dict[str, str], content: str
) -> None:
    response = api.patch(
        status_path(), content=content, headers={**alice, "Content-Type": "application/json"}
    )

    assert_problem(response, 400, "validation-error")


def test_a_status_body_in_the_wrong_content_type_is_415(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    response = api.patch(
        status_path(),
        content=json.dumps({"status": "CANCELLED"}),
        headers={**alice, "Content-Type": "text/plain"},
    )

    assert_problem(response, 415, "unsupported-media-type")


def test_a_malformed_path_id_on_a_status_change_is_400_naming_the_parameter(
    api: httpx.Client, alice: dict[str, str]
) -> None:
    response = api.patch(status_path("not-a-uuid"), json={"status": "CANCELLED"}, headers=alice)

    assert_problem(response, 400, "validation-error")
    assert [error["parameter"] for error in response.json()["errors"]] == ["id"]


def test_an_invalid_status_body_never_invokes_the_api_function(
    api: httpx.Client, alice: dict[str, str], endpoints: dict[str, Any]
) -> None:
    logs = FunctionLogs(api_function_name(endpoints))
    since_ms = int(time.time() * 1000) - 5_000

    rejected = api.patch(status_path(), json={"status": "LOST"}, headers=alice)
    reached = api.patch(status_path(), json={"status": "CANCELLED"}, headers=alice)

    assert_rejected_by_the_gateway(rejected)
    assert_problem(reached, 404, "not-found")
    logged = logs.wait_for(reached.headers["X-Request-Id"], since_ms)
    assert rejected.headers["X-Request-Id"] not in logged


def test_an_invalid_body_never_invokes_the_api_function(
    api: httpx.Client, alice: dict[str, str], endpoints: dict[str, Any]
) -> None:
    logs = FunctionLogs(api_function_name(endpoints))
    since_ms = int(time.time() * 1000) - 5_000

    rejected = post_json(api, alice, {**NEW_ORDER, "totalAmount": 1})
    accepted = post_json(api, alice, NEW_ORDER)

    assert_rejected_by_the_gateway(rejected)
    assert accepted.status_code == 201
    logged = logs.wait_for(accepted.headers["X-Request-Id"], since_ms)
    assert rejected.headers["X-Request-Id"] not in logged
