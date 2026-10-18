"""One order followed end to end by one id, the trace id of its traceparent (ADR 0008).

Where a trace comes from, what never reaches a log line or a span, and the one terminal log line
every message ends with.
"""

import dataclasses
import re
from collections import Counter
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from order_consumer.application.retries import RetryPolicy
from order_consumer.config import Settings
from tests.integration.in_process import group_of, running
from tests.integration.stack import (
    CONSUMER_DATABASE_URL,
    FULFILMENT_TOKEN,
    ConsumerGroup,
    add_fault_rule,
    assert_all_committed,
    assert_nothing_lost,
    handover_requests,
    publish,
    publish_records,
    stored_order,
    wait_for_dead_letters,
)
from tests.integration.traces import TERMINAL_EVENTS, publish_traced, trace_spans
from tests.support.events import order_placed_envelope, order_placed_event
from tests.support.log_lines import LogLines
from tests.support.spans import span_texts

pytestmark = pytest.mark.integration

CUSTOMER_ID = order_placed_envelope(uuid4())["data"]["customerId"]
DATABASE_PASSWORD = re.search(r":([^:@]+)@", CONSUMER_DATABASE_URL).group(1)  # type: ignore[union-attr]
QUICK_RETRIES = RetryPolicy(backoff_base=0.05, backoff_cap=0.2)


def received_line(log_lines: LogLines, order_id: object) -> dict[str, object]:
    [line] = [
        entry
        for entry in log_lines.entries()
        if entry["event"] == "event.received" and entry.get("order_id") == str(order_id)
    ]
    return line


def test_an_event_without_a_traceparent_gets_a_new_trace(
    consumer: ConsumerGroup, log_lines: LogLines, spans: InMemorySpanExporter
) -> None:
    order_id = uuid4()

    publish(consumer.topic, [(order_id, order_placed_event(order_id))])

    assert_nothing_lost([order_id], consumer)
    stored = stored_order(order_id)
    assert stored is not None
    trace_id = stored["trace_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    received = received_line(log_lines, order_id)
    assert (received["trace_source"], received["trace_id"]) == ("new", trace_id)
    trace_spans(spans, trace_id, consumer.topic)
    [request] = handover_requests(order_id)
    assert request["traceparent"].split("-")[1] == trace_id, "the stub continues the new trace"


def test_an_invalid_traceparent_is_replaced_and_never_logged(
    consumer: ConsumerGroup, log_lines: LogLines
) -> None:
    order_id = uuid4()
    marker = f"forged-{uuid4().hex}"
    forged = f'00-{"0" * 32}-{"0" * 16}-01\n{{"event":"{marker}"}}'

    publish_records(
        consumer.topic,
        [
            (
                str(order_id).encode(),
                order_placed_event(order_id),
                [("traceparent", forged.encode())],
            )
        ],
    )

    assert_nothing_lost([order_id], consumer)
    received = received_line(log_lines, order_id)
    assert received["trace_source"] == "replaced"
    stored = stored_order(order_id)
    assert stored is not None and received["trace_id"] == stored["trace_id"]
    assert marker not in log_lines.text()


def test_a_dead_letter_carries_the_trace_the_consumer_started(
    consumer: ConsumerGroup, log_lines: LogLines
) -> None:
    order_id = uuid4()
    invalid = order_placed_event(order_id, total=1)

    publish_records(consumer.topic, [(str(order_id).encode(), invalid, [("traceparent", b"x")])])

    [dead_letter] = wait_for_dead_letters(consumer)
    [written] = [
        entry
        for entry in log_lines.entries()
        if entry["event"] == "dead_letter.written" and entry.get("order_id") == str(order_id)
    ]
    traceparent = dead_letter.header("traceparent")
    assert traceparent is not None, "the invalid traceparent is replaced, not repeated"
    assert traceparent.split("-")[1] == written["trace_id"]


def test_a_dead_letter_keeps_a_valid_traceparent_as_it_came(consumer: ConsumerGroup) -> None:
    order_id = uuid4()

    producer = publish_traced(consumer.topic, order_id, order_placed_event(order_id, total=1))

    [dead_letter] = wait_for_dead_letters(consumer)
    assert dead_letter.header("traceparent") == producer.traceparent, "a replay continues it"


def test_neither_the_token_nor_secrets_nor_the_customer_reach_a_log_line_or_span(
    consumer_settings: Settings, log_lines: LogLines, spans: InMemorySpanExporter
) -> None:
    """A handover with a known token, retried once, and a rejected one (ADR 0008)."""
    settings = dataclasses.replace(consumer_settings, retry_policy=QUICK_RETRIES)
    retried, rejected = uuid4(), uuid4()
    add_fault_rule(retried, status=503)
    add_fault_rule(rejected, status=422)
    publish(
        settings.kafka_topic,
        [(order_id, order_placed_event(order_id)) for order_id in (retried, rejected)],
    )

    with running(settings):
        assert_nothing_lost([retried], group_of(settings))
        wait_for_dead_letters(group_of(settings))
        assert_all_committed(group_of(settings))

    span_text = "\n".join(span_texts(spans.get_finished_spans()))
    assert "handover attempt" in span_text and "POST" in span_text
    for text in (log_lines.text(), span_text):
        assert FULFILMENT_TOKEN not in text
        assert "Bearer" not in text
        assert DATABASE_PASSWORD not in text
        assert CUSTOMER_ID not in text


def test_every_message_logs_exactly_one_terminal_line(
    consumer_settings: Settings, log_lines: LogLines
) -> None:
    """Handed over, redelivered, re-emitted, invalid, conflicting and rejected: one line each, so
    outcomes can be counted from the logs (ADR 0008)."""
    settings = dataclasses.replace(consumer_settings, retry_policy=QUICK_RETRIES)
    handed_over, invalid, rejected = uuid4(), uuid4(), uuid4()
    event_id = uuid4()
    first = order_placed_event(handed_over, event_id)
    conflicting = order_placed_event(handed_over, customerId="5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d")
    add_fault_rule(rejected, status=422)
    messages = [
        (handed_over, first),
        (handed_over, first),  # redelivered
        (handed_over, order_placed_event(handed_over)),  # re-emitted
        (handed_over, conflicting),
        (invalid, order_placed_event(invalid, total=1)),
        (rejected, order_placed_event(rejected)),
    ]
    publish(settings.kafka_topic, messages)

    with running(settings):
        assert_all_committed(group_of(settings))

    terminal = Counter(
        (entry["partition"], entry["offset"])
        for entry in log_lines.entries()
        if entry["event"] in TERMINAL_EVENTS and entry.get("topic") == settings.kafka_topic
    )
    assert len(terminal) == len(messages)
    assert set(terminal.values()) == {1}
    outcomes = Counter(
        entry["event"]
        for entry in log_lines.entries()
        if entry["event"] in TERMINAL_EVENTS and entry.get("topic") == settings.kafka_topic
    )
    assert outcomes == {"handover.completed": 1, "order.duplicate": 2, "dead_letter.written": 3}
