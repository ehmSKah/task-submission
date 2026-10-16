"""Builders for test data and Lambda events, shared by every suite."""

from dataclasses import dataclass
from typing import Any

from orders_api.domain.order import LineItem, Order, OrderStatus


def an_order(**overrides: object) -> Order:
    fields: dict[str, object] = {
        "id": "3f0c2f40-9a5e-4b1a-9a43-1d6c2a7b8e10",
        "owner_id": "alice-sub",
        "status": OrderStatus.PENDING,
        "currency": "EUR",
        "items": (LineItem(sku="ABC-123", quantity=2, unit_price=1999),),
        "created_at": "2026-10-17T09:30:00.123Z",
        "updated_at": "2026-10-17T09:30:00.123Z",
        "version": 1,
    }
    fields.update(overrides)
    return Order(**fields)  # type: ignore[arg-type]


@dataclass
class FakeLambdaContext:
    function_name: str = "orders-api"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:000000000000:function:orders-api"
    aws_request_id: str = "lambda-req-1"
    remaining_time_in_millis: int = 9_000

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_time_in_millis


def rest_event(method: str, path: str, resource: str | None = None) -> dict[str, Any]:
    return {
        "resource": resource or path,
        "path": path,
        "httpMethod": method,
        "headers": {"Accept": "application/json"},
        "multiValueHeaders": {"Accept": ["application/json"]},
        "queryStringParameters": None,
        "multiValueQueryStringParameters": None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "requestId": "gw-req-1",
            "resourcePath": resource or path,
            "httpMethod": method,
            "path": f"/live{path}",
            "stage": "live",
            "identity": {"sourceIp": "203.0.113.7"},
        },
        "body": None,
        "isBase64Encoded": False,
    }
