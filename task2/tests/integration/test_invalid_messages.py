"""The assignment's second test category: invalid messages (ADR 0001, 0006, 0009).

One case per validation layer. Each ends as a dead letter naming the layer and its errors, with
nothing stored, nothing handed over and the offset committed.
"""

import json
import socket
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from tests.integration.stack import (
    ConsumerGroup,
    Record,
    assert_all_committed,
    assert_not_accepted,
    assert_nothing_duplicated,
    assert_nothing_lost,
    publish_records,
    wait_for_dead_letters,
)
from tests.support.events import order_placed_envelope, order_placed_event

pytestmark = pytest.mark.integration

ORIGINAL_HEADERS = [
    ("traceparent", b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"),
    ("producer", b"order-service"),
]


def encoded(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope).encode()


def dead_letter_for(
    consumer: ConsumerGroup, value: bytes, key: bytes | None
) -> tuple[Record, dict[str, Any]]:
    """Publishes one record and returns its dead letter, checked against ADR 0006, and its detail.

    Ends with the standard checks that apply: the offset is committed.
    """
    publish_records(consumer.topic, [(key, value, ORIGINAL_HEADERS)])

    [dead_letter] = wait_for_dead_letters(consumer)
    assert_all_committed(consumer)

    assert (dead_letter.key, dead_letter.value) == (key, value), "the original record, unchanged"
    assert dead_letter.headers[: len(ORIGINAL_HEADERS)] == ORIGINAL_HEADERS
    assert dead_letter.header("dlt.reason") == "invalid"
    assert dead_letter.header("dlt.attempts") == "0"
    source = dead_letter.header("dlt.source")
    assert source is not None and source.startswith(f"{consumer.topic}/")
    assert dead_letter.header("dlt.consumer") == f"{consumer.group}/{socket.gethostname()}"
    for name in ("dlt.source-timestamp", "dlt.failed-at"):
        stamp = dead_letter.header(name)
        assert stamp is not None and stamp.endswith("Z"), name
        datetime.fromisoformat(stamp)
    detail = dead_letter.header("dlt.detail")
    assert detail is not None
    return dead_letter, json.loads(detail)


def pointers(detail: dict[str, Any]) -> list[str]:
    return sorted(error["pointer"] for error in detail["errors"])


def test_an_unreadable_event_is_dead_lettered(consumer: ConsumerGroup) -> None:
    order_id = uuid4()

    dead_letter, detail = dead_letter_for(consumer, b"\xff\xfe not UTF-8", str(order_id).encode())

    assert detail["layer"] == "unreadable"
    assert pointers(detail) == [""]
    assert dead_letter.header("dlt.event-id") is None
    assert dead_letter.header("dlt.order-id") is None
    assert_not_accepted([order_id])


def test_an_event_that_is_not_a_cloudevent_is_dead_lettered(consumer: ConsumerGroup) -> None:
    order_id = uuid4()
    envelope = order_placed_envelope(order_id)
    del envelope["specversion"], envelope["id"]

    dead_letter, detail = dead_letter_for(consumer, encoded(envelope), str(order_id).encode())

    assert detail["layer"] == "not-a-cloudevent"
    assert [error["message"] for error in detail["errors"]] == [
        "'specversion' is a required property",
        "'id' is a required property",
    ]
    assert dead_letter.header("dlt.event-id") is None
    assert dead_letter.header("dlt.order-id") == str(order_id)
    assert_not_accepted([order_id])


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        pytest.param("type", "order.cancelled", id="unknown type"),
        pytest.param("dataschema", "urn:task2:schema:order-placed:2", id="unknown major version"),
    ],
)
def test_an_unsupported_type_or_version_is_dead_lettered(
    consumer: ConsumerGroup, attribute: str, value: str
) -> None:
    order_id, event_id = uuid4(), uuid4()
    envelope = order_placed_envelope(order_id, event_id)
    envelope[attribute] = value

    dead_letter, detail = dead_letter_for(consumer, encoded(envelope), str(order_id).encode())

    assert detail["layer"] == "unsupported-type-or-version"
    assert pointers(detail) == [f"/{attribute}"]
    assert dead_letter.header("dlt.event-id") == str(event_id)
    assert dead_letter.header("dlt.order-id") == str(order_id)
    assert_not_accepted([order_id])


def test_an_event_breaking_the_schema_is_dead_lettered_with_every_error(
    consumer: ConsumerGroup,
) -> None:
    order_id = uuid4()
    envelope = order_placed_envelope(
        order_id,
        customerId="customer-7",
        placedAt="2026-10-17T11:12:03",
        lines=[{"sku": "KNE-ESC-0042", "quantity": 0, "unitPrice": 1999}],
    )

    _, detail = dead_letter_for(consumer, encoded(envelope), str(order_id).encode())

    assert detail["layer"] == "schema-violation"
    assert detail["errorCount"] == 3
    assert pointers(detail) == ["/data/customerId", "/data/lines/0/quantity", "/data/placedAt"]
    assert_not_accepted([order_id])


def test_an_event_breaking_a_business_rule_is_dead_lettered(consumer: ConsumerGroup) -> None:
    order_id = uuid4()

    _, detail = dead_letter_for(
        consumer, order_placed_event(order_id, total=4499), str(order_id).encode()
    )

    assert detail == {
        "layer": "business-rule",
        "errorCount": 1,
        "errors": [
            {"pointer": "/data/total", "message": "4499 is not the sum of the line totals, 4498"}
        ],
    }
    assert_not_accepted([order_id])


def test_an_event_whose_kafka_key_is_not_its_order_id_is_dead_lettered(
    consumer: ConsumerGroup,
) -> None:
    order_id = uuid4()

    dead_letter, detail = dead_letter_for(consumer, order_placed_event(order_id), b"another-order")

    assert detail["layer"] == "business-rule"
    assert [(error["pointer"], error["message"]) for error in detail["errors"]] == [
        ("/data/orderId", "differs from the Kafka key 'another-order'")
    ]
    assert dead_letter.header("dlt.order-id") == str(order_id)
    assert_not_accepted([order_id])


def test_invalid_events_do_not_hold_up_valid_ones(consumer: ConsumerGroup) -> None:
    invalid_ids = [uuid4() for _ in range(3)]
    valid_ids = [uuid4() for _ in range(3)]
    order_ids: list[UUID] = [*invalid_ids, *valid_ids]

    publish_records(
        consumer.topic,
        [
            (
                str(order_id).encode(),
                order_placed_event(order_id, total=1 if order_id in invalid_ids else 4498),
                [],
            )
            for order_id in order_ids
        ],
    )

    dead_letters = wait_for_dead_letters(consumer, count=len(invalid_ids))
    assert sorted(record.header("dlt.order-id") or "" for record in dead_letters) == sorted(
        str(order_id) for order_id in invalid_ids
    )
    assert_nothing_lost(valid_ids, consumer)
    assert_nothing_duplicated(valid_ids)
    assert_not_accepted(invalid_ids)
