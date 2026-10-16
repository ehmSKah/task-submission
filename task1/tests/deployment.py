"""The deployed environment the adapter and integration suites run against (ADR 0011)."""

import json
import os
from pathlib import Path
from typing import Any

import boto3
import pytest

from orders_api.adapters.outbound.dynamodb_orders import to_item
from orders_api.domain.order import Order

ENDPOINTS = Path(__file__).parents[1] / "build" / "endpoints.json"
FLOCI_URL = os.environ.get("FLOCI_URL", "http://localhost:4566")


def load_endpoints() -> dict[str, Any]:
    if not ENDPOINTS.is_file():
        pytest.fail("build/endpoints.json is missing: run `make up` first")
    endpoints: dict[str, Any] = json.loads(ENDPOINTS.read_text())
    return endpoints


def point_aws_sdk_at(endpoints: dict[str, Any]) -> None:
    """boto3 talks to Floci with dummy credentials; on AWS the developer's own are used."""
    os.environ.setdefault("AWS_DEFAULT_REGION", endpoints["region"])
    if endpoints["target"] == "floci":
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        os.environ.setdefault("AWS_ENDPOINT_URL", FLOCI_URL)


def store_order(endpoints: dict[str, Any], order: Order) -> None:
    """Write an order straight into the deployed table, bypassing the API."""
    boto3.resource("dynamodb").Table(endpoints["ordersTable"]).put_item(Item=to_item(order))
