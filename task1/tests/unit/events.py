"""API Gateway events for the order routes, as the gateway would hand them to the function."""

import base64
import json
from typing import Any

from orders_api.adapters.inbound import api_handler
from tests.builders import FakeLambdaContext, rest_event

ALICE = {"principalId": "alice-sub", "sub": "alice-sub", "roles": "USER"}
BOB = {"principalId": "bob-sub", "sub": "bob-sub", "roles": "USER"}
MANAGER = {"principalId": "manager-sub", "sub": "manager-sub", "roles": "ORDER_MANAGER"}
ADMIN = {"principalId": "admin-sub", "sub": "admin-sub", "roles": "ADMIN"}
# A valid token without any recognised role (ADR 0001).
NOBODY = {"principalId": "nobody-sub", "sub": "nobody-sub", "roles": ""}

VALID_ORDER: dict[str, Any] = {
    "currency": "EUR",
    "items": [
        {"sku": "ABC-123", "quantity": 2, "unitPrice": 1999},
        {"sku": "XYZ-9", "quantity": 1, "unitPrice": 500},
    ],
}


def create_order_event(
    body: Any = VALID_ORDER,
    *,
    content_type: str | None = "application/json",
    raw: str | None = None,
    base64_encoded: bool = False,
    authorizer: dict[str, Any] | None = ALICE,
) -> dict[str, Any]:
    """A `POST /api/v1/orders`; `raw` sends a body verbatim instead of `body` as JSON."""
    event = rest_event("POST", "/api/v1/orders")
    text = raw if raw is not None else json.dumps(body)
    if base64_encoded:
        text = base64.b64encode(text.encode()).decode()
    event["body"] = text
    event["isBase64Encoded"] = base64_encoded
    if content_type is not None:
        event["headers"]["Content-Type"] = content_type
        event["multiValueHeaders"]["Content-Type"] = [content_type]
    if authorizer is not None:
        event["requestContext"]["authorizer"] = authorizer
    return event


def get_order_event(order_id: str, authorizer: dict[str, Any] | None = ALICE) -> dict[str, Any]:
    event = rest_event("GET", f"/api/v1/orders/{order_id}", resource="/api/v1/orders/{id}")
    event["pathParameters"] = {"id": order_id}
    if authorizer is not None:
        event["requestContext"]["authorizer"] = authorizer
    return event


def invoke(event: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = api_handler.handler(event, FakeLambdaContext())
    return response


def headers_of(response: dict[str, Any]) -> dict[str, str]:
    """A proxy response's headers, single- and multi-value merged."""
    merged = {k: v for k, v in (response.get("headers") or {}).items()}
    for key, values in (response.get("multiValueHeaders") or {}).items():
        merged[key] = values[-1]
    return merged
