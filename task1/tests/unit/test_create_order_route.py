import io
import json
from typing import Any

import pytest

from orders_api.adapters.inbound.request_checks import MAX_BODY_BYTES
from tests.unit.events import (
    NOBODY,
    VALID_ORDER,
    create_order_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import (
    InMemoryIdempotencyStore,
    InMemoryOrderRepository,
    WireApi,
)

NEW_ID = "0e6bb2c6-0000-4000-8000-0000000000aa"


@pytest.fixture(autouse=True)
def repository(wire_api: WireApi) -> InMemoryOrderRepository:
    return wire_api(ids=[NEW_ID])


def test_creates_the_order_for_the_caller_with_201_and_etag(
    repository: InMemoryOrderRepository,
) -> None:
    response = invoke(create_order_event())

    assert response["statusCode"] == 201
    assert headers_of(response)["Content-Type"] == "application/json"
    assert headers_of(response)["ETag"] == '"1"'
    assert json.loads(response["body"]) == {
        "id": NEW_ID,
        "ownerId": "alice-sub",
        "status": "PENDING",
        "currency": "EUR",
        "items": VALID_ORDER["items"],
        "totalAmount": 4498,
        "createdAt": "2026-10-17T09:30:00.000Z",
        "updatedAt": "2026-10-17T09:30:00.000Z",
        "version": 1,
    }
    assert repository.get(NEW_ID) is not None


def test_created_order_reads_back_with_the_same_body_and_etag() -> None:
    created = invoke(create_order_event())

    read = invoke(get_order_event(NEW_ID))

    assert read["statusCode"] == 200
    assert json.loads(read["body"]) == json.loads(created["body"])
    assert headers_of(read)["ETag"] == '"1"'


@pytest.mark.parametrize(
    ("event", "status", "slug"),
    [
        (create_order_event({**VALID_ORDER, "totalAmount": 1}), 400, "validation-error"),
        (create_order_event(raw="{"), 400, "validation-error"),
        (create_order_event(content_type="text/plain"), 415, "unsupported-media-type"),
        (create_order_event(raw=" " * (MAX_BODY_BYTES + 1)), 413, "payload-too-large"),
    ],
    ids=["unknown-field", "invalid-json", "content-type", "oversized"],
)
def test_invalid_requests_are_problems_and_store_nothing(
    repository: InMemoryOrderRepository, event: dict[str, Any], status: int, slug: str
) -> None:
    response = invoke(event)

    assert response["statusCode"] == status
    body = problem_of(response)
    assert body["type"] == f"urn:problem:orders-api:{slug}"
    assert body["instance"] == "/api/v1/orders"
    assert repository.get(NEW_ID) is None


def test_duplicate_skus_list_each_repeat_in_errors() -> None:
    item = VALID_ORDER["items"][0]

    response = invoke(create_order_event({"currency": "EUR", "items": [item, item]}))

    assert response["statusCode"] == 400
    assert problem_of(response)["errors"] == [
        {"pointer": "/items/1/sku", "detail": "duplicates the SKU at /items/0/sku"}
    ]


def test_missing_authorizer_context_fails_closed_before_the_body_is_read() -> None:
    response = invoke(create_order_event(raw="{", authorizer=None))

    assert response["statusCode"] == 500
    assert problem_of(response)["type"] == "urn:problem:orders-api:internal-error"


def test_access_line_names_the_created_order(log_output: io.StringIO) -> None:
    invoke(create_order_event())

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    access = next(line for line in lines if line["message"] == "request")
    assert access["status"] == 201
    assert access["orderId"] == NEW_ID
    assert access["sub"] == "alice-sub"


def test_a_caller_without_permission_never_leaves_an_idempotency_record(
    idempotency_store: InMemoryIdempotencyStore,
) -> None:
    event = create_order_event(authorizer=NOBODY)
    event["headers"]["Idempotency-Key"] = "retry-1"
    event["multiValueHeaders"]["Idempotency-Key"] = ["retry-1"]

    response = invoke(event)

    assert response["statusCode"] == 403
    assert idempotency_store.records == {}
    assert idempotency_store.saved_in_progress == []
