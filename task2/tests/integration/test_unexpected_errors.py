"""An unexpected exception is a bug for one message (ADR 0006): it is dead-lettered with the
exception's type, and processing goes on. A crash instead would restart onto the same message
forever. A bug that fails message after message trips the bug guard, which pauses the consumer
instead of dead-lettering them all."""

import dataclasses
import json
import threading
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from order_consumer.application.ports import FulfilmentClient, HandoverResult
from order_consumer.config import Settings
from order_consumer.domain.downstream import DownstreamRequest
from tests.integration.in_process import group_of, heartbeat, running
from tests.integration.stack import (
    assert_nothing_duplicated,
    assert_nothing_lost,
    eventually,
    fulfilment_orders,
    lag,
    publish,
    records,
    stored_order,
    wait_for_dead_letters,
)
from tests.support.events import order_placed_event

pytestmark = pytest.mark.integration


class BugFor:
    """The real fulfilment client, except for one order, where a stand-in bug raises."""

    def __init__(self, order_id: UUID, client: FulfilmentClient) -> None:
        self._order_id = order_id
        self._client = client

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        if request.idempotency_key == str(self._order_id):
            raise RuntimeError("a stand-in bug")
        return self._client.hand_over(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class BugForEvery:
    """The real fulfilment client behind a stand-in bug that every order meets, until it is
    fixed."""

    def __init__(self, client: FulfilmentClient) -> None:
        self._client = client
        self.fixed = threading.Event()

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        if not self.fixed.is_set():
            raise RuntimeError("a stand-in bug")
        return self._client.hand_over(request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def test_an_unexpected_exception_is_dead_lettered_and_processing_goes_on(
    consumer_settings: Settings,
) -> None:
    before, buggy, after = uuid4(), uuid4(), uuid4()
    publish(
        consumer_settings.kafka_topic,
        [(order_id, order_placed_event(order_id)) for order_id in (before, buggy, after)],
        partition=0,
    )

    with running(
        consumer_settings,
        wrap_fulfilment=lambda client: cast(FulfilmentClient, BugFor(buggy, client)),
    ):
        [dead_letter] = wait_for_dead_letters(group_of(consumer_settings))
        assert_nothing_lost([before, after], group_of(consumer_settings))

    assert_nothing_duplicated([before, after])
    assert dead_letter.header("dlt.reason") == "unexpected"
    assert dead_letter.header("dlt.order-id") == str(buggy)
    assert dead_letter.header("dlt.attempts") == "1"
    detail = dead_letter.header("dlt.detail")
    assert detail is not None
    assert json.loads(detail) == {"type": "RuntimeError", "message": "a stand-in bug"}
    assert "Traceback" not in detail, "stack traces go to the error log only"
    assert fulfilment_orders(buggy) == []
    stored = stored_order(buggy)
    assert stored is not None and stored["handover_status"] == "awaiting_handover"


def test_five_unexpected_exceptions_in_a_row_trip_the_bug_guard_until_one_is_processed(
    consumer_settings: Settings,
) -> None:
    """The first four are dead-lettered; the fifth is held, and the orders behind it wait in Kafka
    (ADR 0006). Once the cause is gone, the next probe releases the guard."""
    settings = dataclasses.replace(consumer_settings, pause_probe_interval_seconds=1.0)
    orders = [uuid4() for _ in range(6)]
    publish(
        settings.kafka_topic,
        [(order_id, order_placed_event(order_id)) for order_id in orders],
        partition=0,
    )
    bugs: list[BugForEvery] = []

    def with_the_bug(client: FulfilmentClient) -> FulfilmentClient:
        bugs.append(BugForEvery(client))
        return cast(FulfilmentClient, bugs[-1])

    with running(settings, wrap_fulfilment=with_the_bug):
        paused = eventually(
            lambda: heartbeat(settings) or {},
            until=lambda beat: beat.get("state") == "paused",
            what="the bug guard to trip",
        )
        assert sum(lag(group_of(settings)).values()) == 2, "the fifth is held, the sixth waits"
        dead_letters = records(settings.kafka_dead_letter_topic)

        [bug] = bugs
        bug.fixed.set()
        assert_nothing_lost(orders[4:], group_of(settings))

    assert (paused["reason"], paused["error"]) == ("bug-guard", "RuntimeError: a stand-in bug")
    assert {record.header("dlt.order-id") for record in dead_letters} == {
        str(order_id) for order_id in orders[:4]
    }
    assert {record.header("dlt.reason") for record in dead_letters} == {"unexpected"}
    assert len(records(settings.kafka_dead_letter_topic)) == 4, "the fifth was never dead-lettered"
    assert_nothing_duplicated(orders[4:])
    beat = heartbeat(settings)
    assert beat is not None and beat["state"] == "running"
