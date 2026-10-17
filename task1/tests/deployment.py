"""The deployed environment the adapter and integration suites run against (ADR 0011)."""

import os
from typing import Any

import boto3
import pytest

from orders_api.adapters.outbound.dynamodb_orders import to_item
from orders_api.domain.order import Order
from orders_api_infra import endpoints as endpoints_file
from orders_api_infra.config import load_config
from orders_api_infra.preflight import environment_problems

FLOCI_URL = os.environ.get("FLOCI_URL", "http://localhost:4566")


def load_endpoints() -> dict[str, Any]:
    """Whichever environment the file was written for: the suites take theirs from it."""
    try:
        return endpoints_file.read()
    except SystemExit as missing:
        pytest.fail(str(missing))


def point_aws_sdk_at(endpoints: dict[str, Any]) -> None:
    """boto3 talks to Floci with dummy credentials; on AWS the developer's own are used. A shell
    that would send the run elsewhere, e.g. an emulator endpoint on AWS, fails it (ADR 0010)."""
    os.environ.setdefault("AWS_DEFAULT_REGION", endpoints["region"])
    if endpoints["target"] == "floci":
        os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
        os.environ.setdefault("AWS_ENDPOINT_URL", FLOCI_URL)
    problems = environment_problems(load_config(endpoints["env"]), os.environ)
    if problems:
        pytest.fail("; ".join(problems))


def store_order(endpoints: dict[str, Any], order: Order) -> None:
    """Write an order straight into the deployed table, bypassing the API."""
    boto3.resource("dynamodb").Table(endpoints["ordersTable"]).put_item(Item=to_item(order))
