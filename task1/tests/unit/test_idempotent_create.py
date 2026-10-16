"""Retried creates with an `Idempotency-Key` (ADRs 0004 and 0009), against an in-memory store."""

import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.inbound.idempotency import idempotency_key
from tests.builders import FakeLambdaContext
from tests.unit.events import ALICE, VALID_ORDER, create_order_event, headers_of, invoke
from tests.unit.fakes import (
    FixedClock,
    InMemoryIdempotencyStore,
    InMemoryOrderRepository,
    SequentialIds,
)

FIRST_ID = "0e6bb2c6-0000-4000-8000-0000000000a1"
SECOND_ID = "0e6bb2c6-0000-4000-8000-0000000000a2"
BOB = {"principalId": "bob-sub", "sub": "bob-sub", "roles": "USER"}
KEY = "retry-me-7f3a"


@pytest.fixture(autouse=True)
def repository(monkeypatch: pytest.MonkeyPatch) -> InMemoryOrderRepository:
    fake = InMemoryOrderRepository()
    monkeypatch.setattr(api_handler, "_orders", fake)
    monkeypatch.setattr(api_handler, "clock", FixedClock(datetime(2026, 10, 17, 9, 30, tzinfo=UTC)))
    monkeypatch.setattr(api_handler, "order_ids", SequentialIds([FIRST_ID, SECOND_ID]))
    return fake


def create_event(
    body: Any = VALID_ORDER,
    *,
    key: str | None = KEY,
    header: str = "Idempotency-Key",
    authorizer: dict[str, Any] = ALICE,
    raw: str | None = None,
) -> dict[str, Any]:
    event = create_order_event(body, authorizer=authorizer, raw=raw)
    if key is not None:
        event["headers"][header] = key
        event["multiValueHeaders"][header] = [key]
    return event


def create(body: Any = VALID_ORDER, **options: Any) -> dict[str, Any]:
    return invoke(create_event(body, **options))


def problem_type(response: dict[str, Any]) -> str:
    assert headers_of(response)["Content-Type"] == "application/problem+json"
    body: dict[str, Any] = json.loads(response["body"])
    assert body["status"] == response["statusCode"]
    return str(body["type"])


def test_the_same_key_and_body_replays_the_original_201_and_creates_nothing_more(
    repository: InMemoryOrderRepository,
) -> None:
    first = create()

    second = create()

    assert first["statusCode"] == 201
    assert second["statusCode"] == 201
    assert second["body"] == first["body"]
    assert headers_of(second)["ETag"] == headers_of(first)["ETag"] == '"1"'
    assert json.loads(second["body"])["id"] == FIRST_ID
    assert repository.get(SECOND_ID) is None


def test_the_body_fingerprint_ignores_json_formatting_and_key_order() -> None:
    first = create()

    reordered = json.dumps({"items": VALID_ORDER["items"], "currency": "EUR"}, indent=4)
    second = create(raw=reordered)

    assert second["statusCode"] == 201
    assert json.loads(second["body"])["id"] == FIRST_ID
    assert second["body"] == first["body"]


def test_the_same_key_with_a_different_body_is_a_422(repository: InMemoryOrderRepository) -> None:
    create()

    response = create({**VALID_ORDER, "currency": "USD"})

    assert response["statusCode"] == 422
    assert problem_type(response) == "urn:problem:orders-api:idempotency-key-mismatch"
    assert repository.get(SECOND_ID) is None


def test_keys_are_scoped_per_owner(repository: InMemoryOrderRepository) -> None:
    alices = create()

    bobs = create(authorizer=BOB)

    assert bobs["statusCode"] == 201
    order = json.loads(bobs["body"])
    assert order["id"] == SECOND_ID
    assert order["ownerId"] == "bob-sub"
    assert json.loads(alices["body"])["ownerId"] == "alice-sub"


def test_a_different_body_under_another_owners_key_is_not_a_mismatch() -> None:
    create()

    response = create({**VALID_ORDER, "currency": "USD"}, authorizer=BOB)

    assert response["statusCode"] == 201


@pytest.mark.parametrize("header", ["idempotency-key", "IDEMPOTENCY-KEY", "IdEmPoTeNcY-KeY"])
def test_the_header_name_is_matched_in_any_casing(header: str) -> None:
    create()

    response = create(header=header)

    assert json.loads(response["body"])["id"] == FIRST_ID


def test_without_the_header_every_request_creates_an_order(
    idempotency_store: InMemoryIdempotencyStore,
) -> None:
    first = create(key=None)
    second = create(key=None)

    assert {json.loads(first["body"])["id"], json.loads(second["body"])["id"]} == {
        FIRST_ID,
        SECOND_ID,
    }
    assert idempotency_store.records == {}


def test_a_duplicate_while_the_first_is_still_in_progress_is_a_409(
    idempotency_store: InMemoryIdempotencyStore, repository: InMemoryOrderRepository
) -> None:
    create()
    idempotency_store.hold_in_progress()

    response = create()

    assert response["statusCode"] == 409
    assert problem_type(response) == "urn:problem:orders-api:conflict"
    assert repository.get(SECOND_ID) is None


def test_the_in_progress_record_is_bounded_by_the_lambdas_remaining_time(
    idempotency_store: InMemoryIdempotencyStore,
) -> None:
    before_ms = time.time() * 1000

    api_handler.handler(create_event(), FakeLambdaContext(remaining_time_in_millis=4_000))

    (in_progress,) = idempotency_store.saved_in_progress
    assert in_progress.in_progress_expiry_timestamp is not None
    assert before_ms + 4_000 <= in_progress.in_progress_expiry_timestamp <= before_ms + 5_000


def test_the_stored_response_expires_after_24_hours(
    idempotency_store: InMemoryIdempotencyStore,
) -> None:
    before = time.time()

    create()

    (record,) = idempotency_store.records.values()
    assert record.expiry_timestamp is not None
    assert before + 86_400 - 1 <= record.expiry_timestamp <= before + 86_400 + 2


def test_a_failed_create_leaves_the_key_free_for_a_retry(
    monkeypatch: pytest.MonkeyPatch, repository: InMemoryOrderRepository
) -> None:
    class FailingRepository(InMemoryOrderRepository):
        def add(self, order: Any) -> None:
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(api_handler, "_orders", FailingRepository())
    assert create()["statusCode"] == 500

    monkeypatch.setattr(api_handler, "_orders", repository)
    retried = create()

    assert retried["statusCode"] == 201
    assert json.loads(retried["body"])["id"] == SECOND_ID


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Idempotency-Key": "a"}, "a"),
        ({"idempotency-key": "b"}, "b"),
        ({"IDEMPOTENCY-KEY": "c", "Accept": "application/json"}, "c"),
        ({"Accept": "application/json"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_idempotency_key_is_read_from_a_plain_header_mapping_in_any_casing(
    headers: dict[str, str] | None, expected: str | None
) -> None:
    assert idempotency_key(headers) == expected
