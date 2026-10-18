"""The assignment's fourth test category: downstream failures (ADR 0003, 0006, 0009).

The stub's fault rules make the fulfilment service fail for one order at a time. Transient failures
are retried until the handover completes or the retries run out; a refused handover is
dead-lettered at once; a systemic answer stops the consumer with nothing committed.
"""

import dataclasses
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from order_consumer.application.retries import RetryPolicy, SystemicFailure
from order_consumer.config import FulfilmentTimeouts, Settings
from tests.integration.in_process import group_of, run_until_it_fails, running
from tests.integration.stack import (
    Record,
    add_fault_rule,
    assert_all_committed,
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    fulfilment_orders,
    handover_requests,
    handover_statuses,
    lag,
    publish,
    records,
    stored_order,
    wait_for_dead_letters,
)
from tests.integration.traces import named, parent_id, span_id, trace_spans
from tests.support.events import order_placed_event

pytestmark = pytest.mark.integration

# Short enough that a test runs out of retries in about a second.
SHORT_TIMEOUTS = FulfilmentTimeouts(connect=1.0, read=1.0, attempt_deadline=2.0)
QUICK_RETRIES = RetryPolicy(backoff_base=0.05, backoff_cap=0.2)


@pytest.fixture
def settings(consumer_settings: Settings) -> Settings:
    return dataclasses.replace(
        consumer_settings, fulfilment_timeouts=SHORT_TIMEOUTS, retry_policy=QUICK_RETRIES
    )


def publish_order(settings: Settings, order_id: UUID) -> None:
    """Published after the order's fault rule is in place, so the first attempt meets it."""
    publish(settings.kafka_topic, [(order_id, order_placed_event(order_id))])


def handover_status(order_id: UUID) -> str | None:
    stored = stored_order(order_id)
    return None if stored is None else str(stored["handover_status"])


def dead_letter_of(settings: Settings, order_id: UUID) -> tuple[Record, dict[str, Any]]:
    """The order's only dead letter, and its detail; the offset is committed after it."""
    consumers = group_of(settings)
    [dead_letter] = wait_for_dead_letters(consumers)
    assert_all_committed(consumers)
    assert dead_letter.key == str(order_id).encode()
    assert dead_letter.header("dlt.order-id") == str(order_id)
    detail = dead_letter.header("dlt.detail")
    assert detail is not None
    return dead_letter, json.loads(detail)


def assert_handed_over_without_dead_letters(settings: Settings, order_id: UUID) -> None:
    assert_nothing_lost([order_id], group_of(settings))
    assert_nothing_duplicated([order_id])
    assert records(settings.kafka_dead_letter_topic) == []


def test_two_transient_failures_then_success_end_handed_over_after_three_attempts(
    settings: Settings,
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=2, status=503)
    publish_order(settings, order_id)

    with running(settings):
        assert_handed_over_without_dead_letters(settings, order_id)

    assert handover_statuses(order_id) == [503, 503, 201]


def test_each_attempt_is_a_span_of_the_trace_and_sends_its_traceparent(
    settings: Settings, spans: InMemorySpanExporter
) -> None:
    """Retries appear as sibling attempt spans with their status; each request carried the
    traceparent of its own span, so the stub's span sits inside its attempt (ADR 0008)."""
    order_id = uuid4()
    add_fault_rule(order_id, times=2, status=503)
    publish_order(settings, order_id)

    with running(settings):
        assert_handed_over_without_dead_letters(settings, order_id)

    stored = stored_order(order_id)
    assert stored is not None
    found = trace_spans(spans, stored["trace_id"], settings.kafka_topic)
    [message] = named(found, f"process {settings.kafka_topic}")
    attempts = sorted(named(found, "handover attempt"), key=lambda span: span.start_time or 0)
    assert [parent_id(attempt) for attempt in attempts] == [span_id(message)] * 3
    assert [(attempt.attributes or {}).get("handover.status") for attempt in attempts] == [
        503,
        503,
        None,
    ]
    requests = {parent_id(request): request for request in named(found, "POST")}
    sent = [requests[span_id(attempt)] for attempt in attempts]
    # The trace id and the parent span id; the flags of a new trace also mark its id as random.
    assert [entry["traceparent"].split("-")[1:3] for entry in handover_requests(order_id)] == [
        [stored["trace_id"], f"{span_id(request):016x}"] for request in sent
    ]


def test_a_dropped_connection_is_retried(settings: Settings) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, drop_connection=True)
    publish_order(settings, order_id)

    with running(settings):
        assert_handed_over_without_dead_letters(settings, order_id)

    assert handover_statuses(order_id) == [None, 201], "the first answer was broken off"


def test_a_handover_that_keeps_failing_ends_failed_with_a_retries_exhausted_dead_letter(
    settings: Settings,
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=10, status=503)
    publish_order(settings, order_id)

    with running(settings):
        dead_letter, detail = dead_letter_of(settings, order_id)

    assert dead_letter.header("dlt.reason") == "retries-exhausted"
    assert dead_letter.header("dlt.attempts") == "5"
    assert detail == {"status": 503, "error": "503 Service Unavailable"}
    assert handover_status(order_id) == "handover_failed"
    assert handover_statuses(order_id) == [503] * 5
    assert fulfilment_orders(order_id) == []


def test_a_long_retry_after_ends_the_retries_at_once(settings: Settings) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, status=503, retry_after="120")
    publish_order(settings, order_id)

    with running(settings):
        dead_letter, detail = dead_letter_of(settings, order_id)

    assert dead_letter.header("dlt.reason") == "retries-exhausted"
    assert dead_letter.header("dlt.attempts") == "1"
    assert detail["retryAfterSeconds"] == 120.0
    assert handover_status(order_id) == "handover_failed"


@pytest.mark.parametrize("status", [400, 422])
def test_a_rejected_handover_is_dead_lettered_without_retries(
    settings: Settings, status: int
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=10, status=status)
    publish_order(settings, order_id)

    with running(settings):
        dead_letter, detail = dead_letter_of(settings, order_id)

    assert dead_letter.header("dlt.reason") == "rejected"
    assert dead_letter.header("dlt.attempts") == "1"
    assert detail["status"] == status
    assert json.loads(detail["problem"])["status"] == status, "the start of the problem body"
    assert handover_status(order_id) == "handover_failed"
    assert handover_statuses(order_id) == [status]
    assert fulfilment_orders(order_id) == []


def test_a_timeout_then_409_in_progress_then_the_replay_end_with_one_fulfilment_order(
    consumer_settings: Settings,
) -> None:
    """The fulfilment service is still creating the order when the first attempt times out
    (ADR 0003). With the jitter pinned to its longest wait, the attempts start at about 0, 2 and
    4 s, and the stub answers the first one at 3 s: the second meets it in progress, the third
    gets the replay. Each side has about a second of margin."""
    settings = dataclasses.replace(
        consumer_settings,
        fulfilment_timeouts=SHORT_TIMEOUTS,
        retry_policy=RetryPolicy(backoff_base=1.0),
    )
    order_id = uuid4()
    add_fault_rule(order_id, delay_seconds=3)
    publish_order(settings, order_id)

    with running(settings, jitter=lambda longest: longest):
        assert_handed_over_without_dead_letters(settings, order_id)

    # In the order the stub received them; the first was answered last, after its client left.
    assert handover_statuses(order_id) == [201, 409, 201]
    [created] = fulfilment_orders(order_id)
    stored = stored_order(order_id)
    assert stored is not None and stored["fulfilment_order_id"] == created["id"]


def test_a_systemic_answer_stops_the_consumer_with_nothing_committed(settings: Settings) -> None:
    """Until the consumer pauses on systemic failures, it stops; the event waits in Kafka and a
    restarted consumer completes it (ADR 0006)."""
    order_id = uuid4()
    add_fault_rule(order_id, status=404)
    publish_order(settings, order_id)

    failure = run_until_it_fails(settings)

    assert isinstance(failure, SystemicFailure)
    assert failure.attempts == 1, "a systemic answer is not retried"
    assert handover_status(order_id) == "awaiting_handover"
    assert handover_statuses(order_id) == [404]
    assert sum(lag(group_of(settings)).values()) == 1, "nothing committed"
    assert records(settings.kafka_dead_letter_topic) == [], "nothing dead-lettered"

    with running(settings):
        assert_handed_over_without_dead_letters(settings, order_id)
    assert handover_statuses(order_id) == [404, 201]


def test_a_failed_handover_is_completed_when_its_event_comes_again(settings: Settings) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, status=503, retry_after="120")
    event = order_placed_event(order_id)
    publish(settings.kafka_topic, [(order_id, event)])

    with running(settings):
        dead_letter_of(settings, order_id)
        publish(settings.kafka_topic, [(order_id, event)])
        eventually(
            lambda: handover_status(order_id),
            until=lambda status: status == "handed_over",
            what=f"the failed handover of {order_id} to be completed",
        )
        assert_all_committed(group_of(settings))
    assert_nothing_duplicated([order_id])
