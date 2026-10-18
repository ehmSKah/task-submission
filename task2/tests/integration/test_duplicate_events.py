"""The assignment's third test category: duplicate events (ADR 0002, 0009).

A redelivery and a re-emission cause no new business operation; a conflicting event is set aside
as a dead letter and the first accepted version stays; of two consumers racing on one order, one
stores it. In every case there is exactly one order row and one fulfilment order.
"""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from order_consumer.config import Settings
from tests.integration.in_process import (
    Pause,
    crash_after_storing,
    group_of,
    run_until_crash,
    running,
)
from tests.integration.stack import (
    ConsumerGroup,
    assert_all_committed,
    assert_nothing_duplicated,
    assert_nothing_lost,
    create_topic,
    handover_statuses,
    order_row_count,
    processed_events,
    publish,
    stored_order,
    wait_for_dead_letters,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.integration

# The first version has two lines, 4498 in total; this one has another quantity and total.
CONFLICTING = {"lines": [{"sku": "KNE-ESC-0042", "quantity": 3, "unitPrice": 1999}], "total": 5997}


def recorded(order_id: UUID) -> list[tuple[str, str]]:
    return [(str(row["event_id"]), row["outcome"]) for row in processed_events(order_id)]


def stored_total(order_id: UUID) -> int:
    stored = stored_order(order_id)
    assert stored is not None, f"order {order_id} is not stored"
    return int(stored["total"])


def on_another_topic(settings: Settings) -> Settings:
    """Another consumer group on a topic of its own, sharing the database and the stub."""
    topic = f"{settings.kafka_topic}.other"
    create_topic(topic)
    create_topic(f"{topic}.dlt")
    return replace(
        settings,
        kafka_topic=topic,
        kafka_dead_letter_topic=f"{topic}.dlt",
        kafka_group_id=f"{settings.kafka_group_id}-other",
    )


def test_a_redelivery_causes_no_new_business_operation(consumer: ConsumerGroup) -> None:
    order_id, event_id = uuid4(), uuid4()
    event = order_placed_event(order_id, event_id)

    publish(consumer.topic, [(order_id, event), (order_id, event)])

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
    assert handover_statuses(order_id) == [201]
    assert recorded(order_id) == [(str(event_id), "accepted")]


def test_a_re_emission_causes_no_new_business_operation_and_is_recorded(
    consumer: ConsumerGroup,
) -> None:
    order_id, first_event_id, re_emitted_event_id = uuid4(), uuid4(), uuid4()

    publish(
        consumer.topic,
        [
            (order_id, order_placed_event(order_id, first_event_id)),
            (order_id, order_placed_event(order_id, re_emitted_event_id)),
        ],
    )

    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
    assert handover_statuses(order_id) == [201]
    assert recorded(order_id) == [
        (str(first_event_id), "accepted"),
        (str(re_emitted_event_id), "duplicate"),
    ]
    first, re_emitted = processed_events(order_id)
    assert (first["topic"], first["partition"]) == (re_emitted["topic"], re_emitted["partition"])
    assert re_emitted["offset"] == first["offset"] + 1


@pytest.mark.parametrize(
    "same_event_id",
    [pytest.param(False, id="new event id"), pytest.param(True, id="same event id")],
)
def test_a_conflicting_event_is_dead_lettered_and_the_first_version_stays(
    consumer: ConsumerGroup, same_event_id: bool
) -> None:
    order_id, first_event_id = uuid4(), uuid4()
    conflicting_event_id = first_event_id if same_event_id else uuid4()
    conflicting = order_placed_event(order_id, conflicting_event_id, **CONFLICTING)

    publish(
        consumer.topic,
        [(order_id, order_placed_event(order_id, first_event_id)), (order_id, conflicting)],
    )

    [dead_letter] = wait_for_dead_letters(consumer)
    assert_nothing_lost([order_id], consumer)
    assert_nothing_duplicated([order_id])
    assert (dead_letter.key, dead_letter.value) == (str(order_id).encode(), conflicting)
    assert dead_letter.header("dlt.reason") == "conflict"
    assert dead_letter.header("dlt.attempts") == "0"
    assert dead_letter.header("dlt.event-id") == str(conflicting_event_id)
    assert dead_letter.header("dlt.order-id") == str(order_id)
    assert json.loads(dead_letter.header("dlt.detail") or "") == {
        "acceptedEventId": str(first_event_id),
        "fields": ["/data/lines", "/data/total"],
    }
    assert stored_total(order_id) == 4498
    assert handover_statuses(order_id) == [201]
    assert recorded(order_id) == [(str(first_event_id), "accepted")]


@pytest.mark.parametrize(
    "order_exists",
    [pytest.param(False, id="for a new order"), pytest.param(True, id="for a stored order")],
)
def test_an_event_id_already_recorded_for_another_order_is_dead_lettered_as_a_conflict(
    consumer: ConsumerGroup, order_exists: bool
) -> None:
    first_order_id, other_order_id, first_event_id = uuid4(), uuid4(), uuid4()
    accepted = [first_order_id, other_order_id] if order_exists else [first_order_id]
    # The orders' partitions may differ, so the reused event is published once they are handed over.
    publish(consumer.topic, [(order_id, order_placed_event(order_id)) for order_id in accepted[1:]])
    publish(consumer.topic, [(first_order_id, order_placed_event(first_order_id, first_event_id))])
    assert_nothing_lost(accepted, consumer)
    reused = order_placed_event(other_order_id, first_event_id)

    publish(consumer.topic, [(other_order_id, reused)])

    [dead_letter] = wait_for_dead_letters(consumer)
    assert_all_committed(consumer)
    assert dead_letter.value == reused
    assert dead_letter.header("dlt.reason") == "conflict"
    assert json.loads(dead_letter.header("dlt.detail") or "") == {
        "acceptedEventId": str(first_event_id),
        "fields": ["/data/orderId"],
    }
    assert_nothing_duplicated(accepted)
    assert order_row_count(other_order_id) == (1 if order_exists else 0)
    assert str(first_event_id) not in [event_id for event_id, _ in recorded(other_order_id)]


def test_a_conflicting_event_is_not_handed_over_while_the_first_handover_is_unfinished(
    consumer_settings: Settings,
) -> None:
    order_id = uuid4()
    publish(consumer_settings.kafka_topic, [(order_id, order_placed_event(order_id))])
    run_until_crash(consumer_settings, wrap_store=crash_after_storing)

    # The conflicting version reaches another consumer while the first awaits its redelivery.
    elsewhere = on_another_topic(consumer_settings)
    publish(elsewhere.kafka_topic, [(order_id, order_placed_event(order_id, **CONFLICTING))])
    with running(elsewhere):
        [dead_letter] = wait_for_dead_letters(group_of(elsewhere))
        assert_all_committed(group_of(elsewhere))
    assert dead_letter.header("dlt.reason") == "conflict"
    assert handover_statuses(order_id) == [], "the conflicting version is never handed over"

    with running(consumer_settings):
        assert_nothing_lost([order_id], group_of(consumer_settings))
    assert_nothing_duplicated([order_id])
    assert handover_statuses(order_id) == [201]
    assert stored_total(order_id) == 4498


def test_two_consumers_racing_on_one_order_end_with_one_order_and_one_fulfilment_order(
    consumer_settings: Settings,
) -> None:
    order_id, event_id = uuid4(), uuid4()
    publish(consumer_settings.kafka_topic, [(order_id, order_placed_event(order_id, event_id))])
    first = consumer_settings
    second = replace(first, kafka_group_id=f"{first.kafka_group_id}-second")
    pause = Pause()

    with running(first, wrap_store=pause.after_storing):
        pause.wait_until_reached()
        try:
            # The first has stored the order; the second finds it stored and hands it over.
            with running(second):
                assert_nothing_lost([order_id], group_of(second))
        finally:
            pause.release()
        assert_all_committed(group_of(first))

    assert_nothing_duplicated([order_id])
    assert handover_statuses(order_id) == [201, 201], "the first's handover gets the replay"
    assert recorded(order_id) == [(str(event_id), "accepted")]
