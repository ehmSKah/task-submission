"""The assignment's first test category: successful processing (ADR 0009)."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.integration.stack import (
    ConsumerGroup,
    assert_nothing_duplicated,
    assert_nothing_lost,
    fulfilment_orders,
    handover_requests,
    processed_events,
    publish,
    publish_records,
    stored_order,
)
from tests.integration.traces import (
    TERMINAL_EVENTS,
    named,
    new_traceparent,
    parent_id,
    span_id,
    trace_spans,
)
from tests.support.events import order_placed_envelope, order_placed_event
from tests.support.log_lines import LogLines

pytestmark = pytest.mark.integration


def test_a_valid_event_is_stored_handed_over_and_committed(consumer: ConsumerGroup) -> None:
    order_id = uuid4()

    publish(consumer.topic, [(order_id, order_placed_event(order_id))])

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
    stored = stored_order(order_id)
    assert stored is not None
    [fulfilment_order] = fulfilment_orders(order_id)
    assert stored["fulfilment_order_id"] == fulfilment_order["id"]
    assert stored["placed_at"] == datetime(2026, 10, 17, 9, 12, 3, tzinfo=UTC)
    assert (stored["total"], stored["item_count"]) == (4498, 3)


def test_several_events_across_partitions_are_all_handed_over_once(
    consumer: ConsumerGroup,
) -> None:
    order_ids = [uuid4() for _ in range(10)]

    publish(consumer.topic, [(order_id, order_placed_event(order_id)) for order_id in order_ids])

    assert_nothing_lost(order_ids, consumer)
    assert_nothing_duplicated(order_ids)


def test_unknown_fields_inside_v1_are_ignored_and_not_handed_over(consumer: ConsumerGroup) -> None:
    """The stub rejects fields outside the fulfilment contract, so a handover proves they were
    dropped and the request had the fulfilment service's own shape."""
    order_id = uuid4()
    lines = [{"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 2249, "giftWrap": True}]
    envelope = order_placed_envelope(order_id, promoCode="AUTUMN", lines=lines)
    envelope["partitionkey"] = "an extension attribute"

    publish(consumer.topic, [(order_id, json.dumps(envelope).encode())])

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])


def test_a_handover_is_one_trace_from_the_event_to_the_fulfilment_service(
    consumer: ConsumerGroup, spans: InMemorySpanExporter
) -> None:
    """The message's span continues the producer's trace; the database writes and the handover
    attempt are its children, the HTTP request sits inside the attempt, and the stub received the
    request's traceparent (ADR 0008)."""
    order_id = uuid4()
    traceparent, trace_id, producer_span_id = new_traceparent()

    publish_records(
        consumer.topic,
        [
            (
                str(order_id).encode(),
                order_placed_event(order_id),
                [("traceparent", traceparent.encode())],
            )
        ],
    )

    assert_nothing_lost([order_id], consumer)
    found = trace_spans(spans, trace_id, consumer.topic)
    [message] = named(found, f"process {consumer.topic}")
    assert message.kind is trace.SpanKind.CONSUMER
    assert parent_id(message) == int(producer_span_id, 16)
    assert {span.name for span in found if parent_id(span) == span_id(message)} == {
        "INSERT orders order_lines processed_events",
        "handover attempt",
        "UPDATE orders",
    }
    [attempt] = named(found, "handover attempt")
    [request] = named(found, "POST")
    assert request.kind is trace.SpanKind.CLIENT
    assert parent_id(request) == span_id(attempt)
    assert (request.attributes or {})["http.response.status_code"] == 201
    [received] = handover_requests(order_id)
    assert received["traceparent"] == f"00-{trace_id}-{span_id(request):016x}-01"


def test_the_trace_id_is_stored_with_the_order_and_its_event(consumer: ConsumerGroup) -> None:
    order_id = uuid4()
    traceparent, trace_id, _ = new_traceparent()

    publish_records(
        consumer.topic,
        [
            (
                str(order_id).encode(),
                order_placed_event(order_id),
                [("traceparent", traceparent.encode())],
            )
        ],
    )

    assert_nothing_lost([order_id], consumer)
    stored = stored_order(order_id)
    assert stored is not None and stored["trace_id"] == trace_id
    [event] = processed_events(order_id)
    assert event["trace_id"] == trace_id


def test_a_handed_over_order_logs_one_terminal_line_and_every_line_carries_its_trace(
    consumer: ConsumerGroup, log_lines: LogLines
) -> None:
    order_id = uuid4()
    traceparent, trace_id, _ = new_traceparent()

    publish_records(
        consumer.topic,
        [
            (
                str(order_id).encode(),
                order_placed_event(order_id),
                [("traceparent", traceparent.encode())],
            )
        ],
    )

    assert_nothing_lost([order_id], consumer)
    lines = [entry for entry in log_lines.entries() if entry.get("order_id") == str(order_id)]
    assert [entry["event"] for entry in lines if entry["event"] in TERMINAL_EVENTS] == [
        "handover.completed"
    ]
    assert {entry["trace_id"] for entry in lines} == {trace_id}
    [received] = [entry for entry in lines if entry["event"] == "event.received"]
    assert received["trace_source"] == "header"
