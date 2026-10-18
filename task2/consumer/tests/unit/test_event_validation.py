"""Reading an order placed event: the five validation layers of ADR 0001, in order."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from order_consumer.domain.event import OrderPlacedReader
from order_consumer.domain.validation import InvalidEvent, Layer
from tests.support.events import order_placed_envelope
from tests.support.strategies import order_data

SCHEMA = Path(__file__).parents[3] / "schemas" / "order-placed.v1.schema.json"
ORDER_ID = UUID("3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c")
EVENT_ID = UUID("7d2e1f3a-4b5c-4d6e-8f9a-0b1c2d3e4f5a")
KEY = str(ORDER_ID).encode()

reader = OrderPlacedReader.from_file(SCHEMA)


def encoded(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope).encode()


def test_a_valid_event_is_read_with_its_event_id_and_data() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)

    event = reader.read(KEY, encoded(envelope))

    assert event.event_id == EVENT_ID
    assert event.data == envelope["data"]


def rejected(key: bytes | None, value: bytes) -> InvalidEvent:
    with pytest.raises(InvalidEvent) as caught:
        reader.read(key, value)
    return caught.value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b"\xff\xfe{}", id="not UTF-8"),
        pytest.param(b'{"specversion": "1.0",', id="not JSON"),
        pytest.param(b'{"total": NaN}', id="not standard JSON"),
        pytest.param(b"", id="empty"),
    ],
)
def test_bytes_that_are_not_json_text_are_unreadable(value: bytes) -> None:
    invalid = rejected(KEY, value)

    assert invalid.layer is Layer.UNREADABLE
    [error] = invalid.errors
    assert error.pointer == ""


def test_an_envelope_missing_cloudevent_attributes_is_not_a_cloudevent() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)
    del envelope["specversion"], envelope["id"], envelope["type"]

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.NOT_A_CLOUDEVENT
    assert [(error.pointer, error.message) for error in invalid.errors] == [
        ("", "'specversion' is a required property"),
        ("", "'id' is a required property"),
        ("", "'type' is a required property"),
    ]


@pytest.mark.parametrize(
    ("attribute", "value", "pointer"),
    [
        pytest.param("datacontenttype", "application/xml", "/datacontenttype", id="content type"),
        pytest.param("specversion", "0.3", "/specversion", id="spec version"),
        pytest.param("id", "event-1", "/id", id="event id not a UUID"),
        pytest.param("time", "2026-10-17T09:12:04", "/time", id="time without offset"),
    ],
)
def test_a_wrong_cloudevent_attribute_is_not_a_cloudevent(
    attribute: str, value: str, pointer: str
) -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)
    envelope[attribute] = value

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.NOT_A_CLOUDEVENT
    assert [error.pointer for error in invalid.errors] == [pointer]


def test_json_that_is_not_an_object_is_not_a_cloudevent() -> None:
    invalid = rejected(KEY, b'["order.placed"]')

    assert invalid.layer is Layer.NOT_A_CLOUDEVENT
    assert [(error.pointer, error.message) for error in invalid.errors] == [
        ("", "['order.placed'] is not of type 'object'")
    ]


@pytest.mark.parametrize(
    ("attribute", "value", "pointer"),
    [
        pytest.param("type", "order.cancelled", "/type", id="unknown type"),
        pytest.param("dataschema", "urn:task2:schema:order-placed:2", "/dataschema", id="v2"),
    ],
)
def test_an_unknown_type_or_major_version_is_unsupported(
    attribute: str, value: str, pointer: str
) -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)
    envelope[attribute] = value

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.UNSUPPORTED_TYPE_OR_VERSION
    assert [error.pointer for error in invalid.errors] == [pointer]


def test_an_event_without_a_schema_version_is_unsupported() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)
    del envelope["dataschema"]

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.UNSUPPORTED_TYPE_OR_VERSION
    assert [error.message for error in invalid.errors] == ["'dataschema' is a required property"]


def test_checking_stops_at_the_first_layer_that_fails() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, total=-1)
    del envelope["id"]
    envelope["type"] = "order.cancelled"

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.NOT_A_CLOUDEVENT
    assert [error.pointer for error in invalid.errors] == [""]


def test_every_schema_violation_in_the_data_is_reported_with_its_pointer() -> None:
    envelope = order_placed_envelope(
        ORDER_ID,
        EVENT_ID,
        customerId="customer-7",
        placedAt="2026-10-17T11:12:03",
        lines=[
            {"sku": "KNE-ESC-0042", "quantity": 0, "unitPrice": 1999},
            {"sku": "kne-elv-0007", "quantity": 1, "unitPrice": -500},
        ],
    )
    del envelope["data"]["total"]

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.SCHEMA_VIOLATION
    assert sorted((error.pointer, error.message) for error in invalid.errors) == [
        ("/data", "'total' is a required property"),
        ("/data/customerId", "'customer-7' is not a 'uuid'"),
        ("/data/lines/0/quantity", "0 is less than the minimum of 1"),
        ("/data/lines/1/sku", "'kne-elv-0007' does not match '^[A-Z0-9]+(-[A-Z0-9]+)*$'"),
        ("/data/lines/1/unitPrice", "-500 is less than the minimum of 0"),
        ("/data/placedAt", "'2026-10-17T11:12:03' is not a 'date-time'"),
    ]


@pytest.mark.parametrize(
    ("data", "pointer"),
    [
        pytest.param({"lines": []}, "/data/lines", id="no lines"),
        pytest.param(
            {"lines": [{"sku": f"SKU-{n}", "quantity": 1, "unitPrice": 1} for n in range(51)]},
            "/data/lines",
            id="more than 50 lines",
        ),
        pytest.param(
            {"lines": [{"sku": "KNE-ESC-0042", "quantity": 1000, "unitPrice": 1}]},
            "/data/lines/0/quantity",
            id="quantity above 999",
        ),
        pytest.param(
            {"lines": [{"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1_000_001}]},
            "/data/lines/0/unitPrice",
            id="unit price above 1,000,000",
        ),
        pytest.param({"total": "4498"}, "/data/total", id="money as a string"),
        pytest.param({"total": 44.98}, "/data/total", id="money as a fraction"),
        pytest.param({"currency": "eur"}, "/data/currency", id="currency not ISO 4217"),
        pytest.param({"currency": "EUR\n"}, "/data/currency", id="currency with a newline"),
        pytest.param(
            {"lines": [{"sku": "KNE-ESC-0042\n", "quantity": 1, "unitPrice": 4498}]},
            "/data/lines/0/sku",
            id="SKU with a trailing newline",
        ),
    ],
)
def test_values_outside_the_indicative_limits_break_the_schema(
    data: dict[str, Any], pointer: str
) -> None:
    invalid = rejected(KEY, encoded(order_placed_envelope(ORDER_ID, EVENT_ID, **data)))

    assert invalid.layer is Layer.SCHEMA_VIOLATION
    assert pointer in [error.pointer for error in invalid.errors]


def test_an_event_without_data_breaks_the_schema() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)
    del envelope["data"]

    invalid = rejected(KEY, encoded(envelope))

    assert invalid.layer is Layer.SCHEMA_VIOLATION
    assert [(error.pointer, error.message) for error in invalid.errors] == [
        ("", "'data' is a required property")
    ]


def test_at_most_20_errors_are_reported_but_all_are_counted() -> None:
    lines = [{"sku": f"SKU-{n}", "quantity": 0, "unitPrice": 1} for n in range(30)]

    invalid = rejected(KEY, encoded(order_placed_envelope(ORDER_ID, EVENT_ID, lines=lines)))

    assert len(invalid.errors) == 20
    assert invalid.error_count == 30


def test_quoted_values_are_cut_to_64_characters() -> None:
    sku = "x" * 500
    lines = [{"sku": sku, "quantity": 1, "unitPrice": 1}]

    invalid = rejected(KEY, encoded(order_placed_envelope(ORDER_ID, EVENT_ID, lines=lines)))

    messages = [error.message for error in invalid.errors]
    assert sorted(messages) == [
        f"'{'x' * 62}… does not match '^[A-Z0-9]+(-[A-Z0-9]+)*$'",
        f"'{'x' * 62}… is too long",
    ]


def test_unknown_fields_inside_v1_are_ignored() -> None:
    envelope = order_placed_envelope(
        ORDER_ID,
        EVENT_ID,
        promoCode="AUTUMN",
        lines=[{"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 4498, "giftWrap": True}],
    )
    envelope["partitionkey"] = "an extension attribute"

    event = reader.read(KEY, encoded(envelope))

    assert event.event_id == EVENT_ID


def business_errors(key: bytes | None, envelope: dict[str, Any]) -> list[tuple[str, str]]:
    invalid = rejected(key, encoded(envelope))
    assert invalid.layer is Layer.BUSINESS_RULE
    return [(error.pointer, error.message) for error in invalid.errors]


def test_a_total_other_than_the_sum_of_the_line_totals_breaks_a_business_rule() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, total=4499)

    assert business_errors(KEY, envelope) == [
        ("/data/total", "4499 is not the sum of the line totals, 4498")
    ]


def test_a_sku_twice_in_one_order_breaks_a_business_rule() -> None:
    lines = [
        {"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1000},
        {"sku": "KNE-ELV-0007", "quantity": 1, "unitPrice": 1000},
        {"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1000},
    ]
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, lines=lines, total=4000)

    assert business_errors(KEY, envelope) == [
        ("/data/lines/2/sku", "'KNE-ESC-0042' appears in more than one line")
    ]


def test_a_currency_outside_the_allow_list_breaks_a_business_rule() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, currency="XYZ")

    assert business_errors(KEY, envelope) == [
        ("/data/currency", "'XYZ' is not an allowed currency")
    ]


@pytest.mark.parametrize(
    ("key", "message"),
    [
        pytest.param(
            b"another-order", "differs from the Kafka key 'another-order'", id="another key"
        ),
        pytest.param(
            str(ORDER_ID).upper().encode(),
            f"differs from the Kafka key '{str(ORDER_ID).upper()}'",
            id="same UUID, other bytes",
        ),
        pytest.param(None, "differs from the Kafka key: the record has none", id="no key"),
    ],
)
def test_a_kafka_key_other_than_the_order_id_breaks_a_business_rule(
    key: bytes | None, message: str
) -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID)

    assert business_errors(key, envelope) == [("/data/orderId", message)]


def test_every_broken_business_rule_is_reported() -> None:
    lines = [
        {"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1000},
        {"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1000},
    ]
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, currency="XYZ", lines=lines, total=1)

    assert [pointer for pointer, _ in business_errors(b"other", envelope)] == [
        "/data/orderId",
        "/data/currency",
        "/data/lines/1/sku",
        "/data/total",
    ]


def test_an_invalid_event_names_its_event_and_order_ids_when_they_can_be_read() -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID, total=1)

    invalid = rejected(KEY, encoded(envelope))

    assert (invalid.event_id, invalid.order_id) == (EVENT_ID, ORDER_ID)


@pytest.mark.parametrize(
    ("change", "event_id", "order_id"),
    [
        pytest.param({"id": "event-1"}, None, ORDER_ID, id="event id not a UUID"),
        pytest.param({"data": {"orderId": 7}}, EVENT_ID, None, id="order id not a UUID"),
        pytest.param({"data": "an order"}, EVENT_ID, None, id="data not an object"),
    ],
)
def test_ids_that_cant_be_read_are_left_out(
    change: dict[str, Any], event_id: UUID | None, order_id: UUID | None
) -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID) | change

    invalid = rejected(KEY, encoded(envelope))

    assert (invalid.event_id, invalid.order_id) == (event_id, order_id)


def test_an_unreadable_event_has_no_ids() -> None:
    invalid = rejected(KEY, b"\xff")

    assert (invalid.event_id, invalid.order_id) == (None, None)


@given(data=order_data())
def test_any_event_within_the_contract_is_read(data: dict[str, Any]) -> None:
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID) | {"data": data}

    event = reader.read(data["orderId"].encode(), encoded(envelope))

    assert event.data == data


@given(data=order_data(), difference=st.integers(min_value=1, max_value=10**6))
def test_any_total_other_than_the_sum_is_invalid(data: dict[str, Any], difference: int) -> None:
    data["total"] += difference
    envelope = order_placed_envelope(ORDER_ID, EVENT_ID) | {"data": data}

    invalid = rejected(data["orderId"].encode(), encoded(envelope))

    assert invalid.layer is Layer.BUSINESS_RULE
    assert [error.pointer for error in invalid.errors] == ["/data/total"]


def test_the_schema_files_examples_are_read() -> None:
    for example in json.loads(SCHEMA.read_text())["examples"]:
        reader.read(example["data"]["orderId"].encode(), encoded(example))
