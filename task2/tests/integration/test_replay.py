"""Replaying dead letters once their cause is fixed (ADR 0006, 0008).

The replay command runs in the test process, reading the test's own dead-letter topic and
republishing to its own topic, while an in-process consumer processes what it republishes.
"""

import dataclasses
import io
import json
from uuid import UUID, uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from order_consumer import replay
from order_consumer.application.retries import RetryPolicy
from order_consumer.config import FulfilmentTimeouts, Settings
from tests.integration.in_process import group_of, running
from tests.integration.stack import (
    Record,
    add_fault_rule,
    assert_all_committed,
    eventually,
    fulfilment_orders,
    handover_statuses,
    publish,
    records,
    stored_order,
    wait_for_dead_letters,
    wait_until_handed_over,
)
from tests.integration.traces import publish_traced
from tests.support.events import order_placed_envelope, order_placed_event
from tests.support.spans import in_trace

pytestmark = pytest.mark.integration

SHORT_TIMEOUTS = FulfilmentTimeouts(connect=1.0, read=1.0, attempt_deadline=2.0)
QUICK_RETRIES = RetryPolicy(backoff_base=0.05, backoff_cap=0.2)
# Unlike the first version: other lines, another total (ADR 0002).
CONFLICTING = {
    "lines": [{"sku": "KNE-ESC-0042", "quantity": 1, "unitPrice": 1999}],
    "total": 1999,
}


@pytest.fixture
def settings(consumer_settings: Settings) -> Settings:
    return dataclasses.replace(
        consumer_settings, fulfilment_timeouts=SHORT_TIMEOUTS, retry_policy=QUICK_RETRIES
    )


@pytest.fixture
def replay_settings(settings: Settings) -> replay.ReplaySettings:
    """Reading the test's dead-letter topic with a group of its own, republishing to its topic."""
    return replay.ReplaySettings(
        kafka_bootstrap_servers=settings.kafka_bootstrap_servers,
        kafka_topic=settings.kafka_topic,
        kafka_dead_letter_topic=settings.kafka_dead_letter_topic,
        kafka_group_id=f"order-replay-test-{uuid4().hex[:12]}",
    )


def run_replay(settings: replay.ReplaySettings, *args: str) -> list[str]:
    """Runs the command to completion; returns the lines it printed."""
    out = io.StringIO()
    assert replay.run(list(args), settings, out) == 0, out.getvalue()
    return out.getvalue().splitlines()


def line_for(lines: list[str], order_id: UUID) -> str:
    [line] = [line for line in lines if str(order_id) in line]
    return line


def retries_exhausted(settings: Settings, order_id: UUID) -> None:
    """The handover fails five times, so the order ends failed and dead-lettered; the stub's
    fault rule is then used up: the fault is cleared."""
    eventually(
        lambda: stored_order(order_id),
        until=lambda row: row is not None and row["handover_status"] == "handover_failed",
        what=f"order {order_id} to end failed",
    )
    wait_for_dead_letters(group_of(settings))
    assert handover_statuses(order_id) == [503] * 5


def replayed(records_on_topic: list[Record]) -> list[Record]:
    return [record for record in records_on_topic if record.header("replayed-from") is not None]


def test_a_retries_exhausted_dead_letter_replayed_after_the_fault_is_handed_over_in_its_trace(
    settings: Settings, replay_settings: replay.ReplaySettings, spans: InMemorySpanExporter
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=5, status=503)
    producer = publish_traced(settings.kafka_topic, order_id)

    with running(settings):
        retries_exhausted(settings, order_id)
        [dead_letter] = records(settings.kafka_dead_letter_topic)

        lines = run_replay(replay_settings, "--execute")

        wait_until_handed_over(order_id)
        assert_all_committed(group_of(settings))

    assert line_for(lines, order_id).endswith("replayed")
    [original, republished] = records(settings.kafka_topic)
    assert (republished.key, republished.value) == (original.key, original.value)
    assert republished.headers == [
        ("traceparent", producer.traceparent.encode()),
        ("replayed-from", dead_letter.position.encode()),
    ]
    assert len(fulfilment_orders(order_id)) == 1
    stored = stored_order(order_id)
    assert stored is not None and stored["trace_id"] == producer.trace_id
    process_spans = eventually(
        lambda: [
            span
            for span in in_trace(spans.get_finished_spans(), producer.trace_id)
            if span.name == f"process {settings.kafka_topic}"
        ],
        until=lambda found: len(found) == 2,
        what="the first delivery's and the replay's message spans",
    )
    assert [(span.attributes or {}).get("outcome") for span in process_spans] == [
        "dead_lettered",
        "handed_over",
    ]


def test_a_dry_run_lists_what_it_would_replay_and_changes_nothing(
    settings: Settings, replay_settings: replay.ReplaySettings
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=5, status=503)
    publish(settings.kafka_topic, [(order_id, order_placed_event(order_id))])

    with running(settings):
        retries_exhausted(settings, order_id)

        lines = run_replay(replay_settings)

    line = line_for(lines, order_id)
    assert "retries-exhausted" in line and line.endswith("would replay")
    assert lines[-1] == (
        f"dry run: 1 to replay, 0 skipped; --execute publishes them to {settings.kafka_topic}"
    )
    assert len(records(settings.kafka_topic)) == 1, "nothing republished"
    stored = stored_order(order_id)
    assert stored is not None and stored["handover_status"] == "handover_failed"
    assert fulfilment_orders(order_id) == []


def test_replaying_the_same_dead_letter_twice_causes_no_second_fulfilment_order(
    settings: Settings, replay_settings: replay.ReplaySettings
) -> None:
    order_id = uuid4()
    add_fault_rule(order_id, times=5, status=503)
    publish(settings.kafka_topic, [(order_id, order_placed_event(order_id))])

    with running(settings):
        retries_exhausted(settings, order_id)

        run_replay(replay_settings, "--execute", "--order-id", str(order_id))
        wait_until_handed_over(order_id)
        run_replay(replay_settings, "--execute", "--order-id", str(order_id))

        assert_all_committed(group_of(settings))

    assert len(replayed(records(settings.kafka_topic))) == 2
    assert len(fulfilment_orders(order_id)) == 1
    assert handover_statuses(order_id) == [503] * 5 + [201], "the second replay is skipped"


def test_invalid_and_conflicting_dead_letters_are_skipped_unless_forced(
    settings: Settings, replay_settings: replay.ReplaySettings
) -> None:
    schema_violation, conflict, unsupported = uuid4(), uuid4(), uuid4()
    unsupported_version = order_placed_envelope(unsupported)
    unsupported_version["dataschema"] = "urn:task2:schema:order-placed:2"
    publish(
        settings.kafka_topic,
        [
            (schema_violation, order_placed_event(schema_violation, customerId="customer-7")),
            (conflict, order_placed_event(conflict)),
            (conflict, order_placed_event(conflict, **CONFLICTING)),
            (unsupported, encoded(unsupported_version)),
        ],
    )

    with running(settings):
        wait_for_dead_letters(group_of(settings), count=3)
        assert_all_committed(group_of(settings))

        lines = run_replay(replay_settings, "--execute")
        forced = run_replay(replay_settings, "--execute", "--force", "--reason", "conflict")

        assert_all_committed(group_of(settings))

    skipped = "skipped: unchanged bytes fail again; --force replays it"
    assert line_for(lines, schema_violation).endswith(skipped)
    assert line_for(lines, conflict).endswith(skipped)
    assert line_for(lines, unsupported).endswith("replayed")
    assert lines[-1] == f"replayed 1 to {settings.kafka_topic}, 2 skipped"
    assert line_for(forced, conflict).endswith("replayed")
    # Sorted: the two keys may sit on different partitions, read in no particular order. Which
    # run replayed which is shown by the lines above.
    assert sorted(record.key for record in replayed(records(settings.kafka_topic))) == sorted(
        [str(unsupported).encode(), str(conflict).encode()]
    )


def encoded(envelope: dict[str, object]) -> bytes:
    return json.dumps(envelope).encode()
