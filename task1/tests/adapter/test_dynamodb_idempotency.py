"""Powertools Idempotency on the deployed idempotency table (ADRs 0004, 0005 and 0009).

These tests prove the conditional writes Powertools relies on behave on the emulator as they do on
DynamoDB: a fresh key, an expired record and an in-progress record past the Lambda's remaining
time are taken over; anything else is refused.
"""

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import boto3
import pytest
from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)

from orders_api.adapters.inbound.idempotency import KEY_PREFIX, dynamodb_store, run_once
from orders_api.domain.order import LineItem, NewOrder
from tests.builders import FakeLambdaContext

OWNER = "alice-sub"
REQUEST = NewOrder(currency="EUR", items=(LineItem(sku="ABC-123", quantity=2, unit_price=1999),))
OTHER_REQUEST = NewOrder(currency="USD", items=REQUEST.items)


class CountingCreate:
    """A stand-in for the create use case that answers a numbered `201`."""

    def __init__(self, inside: Callable[[], None] = lambda: None) -> None:
        self.calls = 0
        self._inside = inside

    def __call__(self) -> Response[str]:
        self.calls += 1
        self._inside()
        return Response(
            status_code=201,
            content_type="application/json",
            body=json.dumps({"call": self.calls}),
            headers={"ETag": '"1"'},
        )


def record_id(owner: str, key: str) -> str:
    """The table key Powertools derives: the prefix and a SHA-256 of `{owner, key}`."""
    digest = hashlib.sha256(json.dumps({"key": key, "owner": owner}, sort_keys=True).encode())
    return f"{KEY_PREFIX}#{digest.hexdigest()}"


@pytest.fixture
def table(endpoints: dict[str, Any]) -> Any:
    return boto3.resource("dynamodb").Table(endpoints["idempotencyTable"])


@pytest.fixture
def run(endpoints: dict[str, Any]) -> Callable[..., Response[str]]:
    store = dynamodb_store(endpoints["idempotencyTable"])

    def run(
        create: Callable[[], Response[str]],
        key: str,
        *,
        request: NewOrder = REQUEST,
        owner: str = OWNER,
        remaining_ms: int = 9_000,
    ) -> Response[str]:
        return run_once(
            create,
            store=store,
            lambda_context=FakeLambdaContext(remaining_time_in_millis=remaining_ms),
            owner_id=owner,
            key=key,
            request=request,
        )

    return run


def test_ttl_is_enabled_on_expiration(endpoints: dict[str, Any]) -> None:
    description = boto3.client("dynamodb").describe_time_to_live(
        TableName=endpoints["idempotencyTable"]
    )["TimeToLiveDescription"]

    assert description["TimeToLiveStatus"] == "ENABLED"
    assert description["AttributeName"] == "expiration"


def test_a_repeat_replays_the_stored_response_without_running_again(
    run: Callable[..., Response[str]],
) -> None:
    create, key = CountingCreate(), str(uuid.uuid4())

    first = run(create, key)
    second = run(create, key)

    assert create.calls == 1
    assert (second.status_code, second.headers, second.body) == (
        first.status_code,
        first.headers,
        first.body,
    )


def test_the_completed_record_keeps_the_response_for_24_hours(
    run: Callable[..., Response[str]], table: Any
) -> None:
    key, before = str(uuid.uuid4()), time.time()

    run(CountingCreate(), key, remaining_ms=4_000)

    item = table.get_item(Key={"id": record_id(OWNER, key)}, ConsistentRead=True)["Item"]
    assert item["status"] == "COMPLETED"
    assert before + 86_400 - 1 <= int(item["expiration"]) <= time.time() + 86_400 + 1
    assert (
        int(before * 1000) + 4_000
        <= int(item["in_progress_expiration"])
        <= time.time() * 1000 + 4_000
    )
    assert json.loads(item["data"])["statusCode"] == 201
    assert item["validation"]


def test_a_different_request_under_the_same_key_is_refused(
    run: Callable[..., Response[str]],
) -> None:
    create, key = CountingCreate(), str(uuid.uuid4())
    run(create, key)

    with pytest.raises(IdempotencyValidationError):
        run(create, key, request=OTHER_REQUEST)

    assert create.calls == 1


def test_the_same_key_from_another_owner_runs_on_its_own(
    run: Callable[..., Response[str]],
) -> None:
    create, key = CountingCreate(), str(uuid.uuid4())
    run(create, key)

    run(create, key, owner="bob-sub", request=OTHER_REQUEST)

    assert create.calls == 2


def test_a_duplicate_during_the_first_run_is_refused_as_in_progress(
    run: Callable[..., Response[str]],
) -> None:
    key = str(uuid.uuid4())
    duplicate_outcome: list[BaseException] = []

    def duplicate() -> None:
        with pytest.raises(IdempotencyAlreadyInProgressError) as raised:
            run(CountingCreate(), key)
        duplicate_outcome.append(raised.value)

    first = CountingCreate(inside=duplicate)
    run(first, key)

    assert first.calls == 1
    assert len(duplicate_outcome) == 1


def test_an_in_progress_record_past_the_lambdas_remaining_time_is_taken_over(
    run: Callable[..., Response[str]],
) -> None:
    key = str(uuid.uuid4())
    taken_over = CountingCreate()

    def after_the_timeout() -> None:
        time.sleep(0.05)
        run(taken_over, key)

    run(CountingCreate(inside=after_the_timeout), key, remaining_ms=0)

    assert taken_over.calls == 1


def test_an_expired_record_is_overwritten(run: Callable[..., Response[str]], table: Any) -> None:
    create, key = CountingCreate(), str(uuid.uuid4())
    run(create, key)
    table.update_item(
        Key={"id": record_id(OWNER, key)},
        UpdateExpression="SET expiration = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 10},
    )

    rerun = run(create, key, request=OTHER_REQUEST)

    assert create.calls == 2
    assert json.loads(str(rerun.body)) == {"call": 2}


def test_a_failed_run_deletes_its_record(run: Callable[..., Response[str]], table: Any) -> None:
    key = str(uuid.uuid4())

    def failing() -> Response[str]:
        raise RuntimeError("the create failed")

    with pytest.raises(RuntimeError):
        run(failing, key)

    assert "Item" not in table.get_item(Key={"id": record_id(OWNER, key)}, ConsistentRead=True)
