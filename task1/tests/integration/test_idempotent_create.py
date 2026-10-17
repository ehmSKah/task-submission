"""Retried creates with an `Idempotency-Key` through the real path (ADRs 0004 and 0009).

The in-progress `409` depends on timing end to end, so it is covered by unit tests (ADR 0011).
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from orders_api.domain.order import format_timestamp
from tests.integration.assertions import assert_contract_headers, assert_problem
from tests.integration.requests import NEW_ORDER, bearer, subject_of

ORDERS = "/api/v1/orders"


def with_key(token: str, key: str, header: str = "Idempotency-Key") -> dict[str, str]:
    return {**bearer(token), header: key}


def order_ids_created_since(endpoints: dict[str, Any], owner: str, created_at: str) -> list[str]:
    """The owner's orders created from `created_at` until now, read from the `byOwner` index.

    The upper bound keeps out orders other tests wrote straight to the table with a fixed,
    possibly future, timestamp; tables persist across runs.
    """
    table = boto3.resource("dynamodb").Table(endpoints["ordersTable"])
    until = format_timestamp(datetime.now(UTC)) + "~"  # `~` sorts after `#<id>`
    items = table.query(
        IndexName="byOwner",
        KeyConditionExpression=Key("ownerId").eq(owner)
        & Key("createdAtId").between(created_at, until),
    )["Items"]
    return [str(item["id"]) for item in items]


def wait_for_order_ids(
    endpoints: dict[str, Any], owner: str, created_at: str, expected_id: str
) -> list[str]:
    """Lists read an index, so poll until the expected order shows up (ADR 0011)."""
    deadline = time.monotonic() + 5
    while True:
        ids = order_ids_created_since(endpoints, owner, created_at)
        if expected_id in ids or time.monotonic() > deadline:
            return ids
        time.sleep(0.2)


def test_a_retry_with_the_same_key_and_body_replays_the_original_201(
    api: httpx.Client, token_for: Callable[[str], str], endpoints: dict[str, Any]
) -> None:
    token, key = token_for("alice"), str(uuid.uuid4())

    first = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token, key))
    second = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token, key))

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["ETag"] == first.headers["ETag"]
    assert_contract_headers(second)
    order = first.json()
    created = wait_for_order_ids(endpoints, subject_of(token), order["createdAt"], order["id"])
    assert created == [order["id"]]


def test_the_same_key_with_a_different_body_is_a_422(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    token, key = token_for("alice"), str(uuid.uuid4())
    assert api.post(ORDERS, json=NEW_ORDER, headers=with_key(token, key)).status_code == 201

    response = api.post(ORDERS, json={**NEW_ORDER, "currency": "GBP"}, headers=with_key(token, key))

    assert_problem(response, 422, "idempotency-key-mismatch")


def test_keys_are_isolated_per_owner(api: httpx.Client, token_for: Callable[[str], str]) -> None:
    key = str(uuid.uuid4())
    alices = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token_for("alice"), key)).json()

    bobs = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token_for("bob"), key))

    assert bobs.status_code == 201
    assert bobs.json()["id"] != alices["id"]
    assert bobs.json()["ownerId"] == subject_of(token_for("bob"))


def test_the_header_name_is_matched_in_any_casing(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    token, key = token_for("alice"), str(uuid.uuid4())

    first = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token, key, "idempotency-key"))
    second = api.post(ORDERS, json=NEW_ORDER, headers=with_key(token, key, "IDEMPOTENCY-KEY"))

    assert first.status_code == second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


def test_without_the_header_each_request_creates_an_order(
    api: httpx.Client, token_for: Callable[[str], str]
) -> None:
    headers = bearer(token_for("alice"))

    first = api.post(ORDERS, json=NEW_ORDER, headers=headers).json()
    second = api.post(ORDERS, json=NEW_ORDER, headers=headers).json()

    assert first["id"] != second["id"]
