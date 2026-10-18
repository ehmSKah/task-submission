"""An unexpected exception is a bug for one message (ADR 0006): it is dead-lettered with the
exception's type, and processing goes on. A crash instead would restart onto the same message
forever."""

import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from order_consumer.application.ports import FulfilmentClient, HandoverResult
from order_consumer.config import Settings
from order_consumer.domain.downstream import DownstreamRequest
from tests.integration.in_process import group_of, running
from tests.integration.stack import (
    assert_nothing_duplicated,
    assert_nothing_lost,
    fulfilment_orders,
    publish,
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
