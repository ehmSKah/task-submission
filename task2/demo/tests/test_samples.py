"""The sample events `make publish` and `make demo` send, checked against the consumer's own
reading of an event (ADR 0001), so a sample can't drift from what it claims to be."""

import json
import re
from pathlib import Path

import pytest

from demo.samples import INVALID_KINDS, Kind, Message, sample
from order_consumer.domain.event import OrderPlacedEvent, OrderPlacedReader
from order_consumer.domain.validation import InvalidEvent, Layer

READER = OrderPlacedReader.from_file(
    Path(__file__).parents[2] / "schemas" / "order-placed.v1.schema.json"
)
TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-01")


def read(message: Message) -> OrderPlacedEvent:
    return READER.read(message.key, message.value)


def traceparent(message: Message) -> str:
    [value] = [value.decode() for name, value in message.headers if name == "traceparent"]
    return value


@pytest.mark.parametrize("currency", ["EUR", "JPY", "KWD"])
def test_a_valid_sample_is_one_event_the_consumer_accepts(currency: str) -> None:
    valid = sample(Kind.VALID, currency=currency)

    [message] = valid.messages
    event = read(message)
    assert event.data["orderId"] == str(valid.order_id)
    assert event.data["currency"] == currency
    assert event.data["total"] == 4498
    assert message.key == str(valid.order_id).encode()
    assert message.event_id == event.event_id


def test_the_invalid_kinds_are_the_five_validation_layers_in_order() -> None:
    assert [kind.value for kind in INVALID_KINDS] == [layer.value for layer in Layer]


@pytest.mark.parametrize("kind", INVALID_KINDS, ids=lambda kind: kind.value)
def test_an_invalid_sample_fails_at_exactly_its_layer(kind: Kind) -> None:
    invalid = sample(kind)

    [message] = invalid.messages
    with pytest.raises(InvalidEvent) as raised:
        read(message)
    assert raised.value.layer is Layer(kind.value)
    assert message.key == str(invalid.order_id).encode(), "keyed by its order, like every event"


def test_no_invalid_sample_fails_on_its_customer_id() -> None:
    """Validation messages quote values, and a customer id is never shown (ADR 0008)."""
    for kind in INVALID_KINDS:
        [message] = sample(kind).messages
        with pytest.raises(InvalidEvent) as raised:
            read(message)
        assert all("customerId" not in error.pointer for error in raised.value.errors), kind


def test_a_redelivery_is_the_same_record_twice() -> None:
    redelivered = sample(Kind.REDELIVERED)

    first, again = redelivered.messages
    assert first == again
    assert read(first).data["orderId"] == str(redelivered.order_id)


def test_a_re_emission_is_the_same_order_again_under_a_new_event_id() -> None:
    re_emitted = sample(Kind.RE_EMITTED)

    first, again = re_emitted.messages
    assert read(first).data == read(again).data
    assert read(first).event_id != read(again).event_id
    assert first.trace_id != again.trace_id, "sent again by the producer: a new trace"


def test_a_conflicting_event_is_the_same_order_with_other_lines_and_total() -> None:
    conflicting = sample(Kind.CONFLICTING)

    first, other = (read(message).data for message in conflicting.messages)
    assert first["orderId"] == other["orderId"] == str(conflicting.order_id)
    assert first["lines"] != other["lines"]
    assert first["total"] != other["total"]


def test_every_message_carries_a_w3c_traceparent_naming_its_trace() -> None:
    for kind in Kind:
        for message in sample(kind).messages:
            match = TRACEPARENT.fullmatch(traceparent(message))
            assert match is not None, kind
            assert match.group(1) == message.trace_id


def test_every_sample_is_a_new_order_in_a_new_trace() -> None:
    first, second = sample(Kind.VALID), sample(Kind.VALID)

    assert first.order_id != second.order_id
    assert first.messages[0].trace_id != second.messages[0].trace_id
    assert first.messages[0].event_id != second.messages[0].event_id


def test_an_unreadable_sample_is_not_json() -> None:
    [message] = sample(Kind.UNREADABLE).messages

    assert message.value is not None
    with pytest.raises(ValueError):
        json.loads(message.value)
