"""The poll loop commits an offset only once its message reached a terminal outcome (ADR 0004)."""

import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from confluent_kafka import KafkaError

from order_consumer.adapters.inbound.poll_loop import PollLoop
from order_consumer.application.processing import Delivery, Outcome


@dataclass
class FakeMessage:
    offset_: int
    error_: KafkaError | None = None

    def error(self) -> KafkaError | None:
        return self.error_

    def key(self) -> bytes:
        return b"order"

    def value(self) -> bytes:
        return b"{}"

    def topic(self) -> str:
        return "orders"

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return self.offset_


@dataclass
class FakeKafkaConsumer:
    """Hands out its messages once, then asks the loop to stop."""

    messages: list[FakeMessage]
    stop: threading.Event
    timeline: list[str]
    closed: bool = False

    def subscribe(self, topics: list[str], **_callbacks: Any) -> None:
        pass

    def poll(self, timeout: float) -> FakeMessage | None:
        if not self.messages:
            self.stop.set()
            return None
        return self.messages.pop(0)

    def commit(self, message: FakeMessage, asynchronous: bool) -> None:
        assert asynchronous is False, "commits must be synchronous"
        self.timeline.append(f"commit {message.offset()}")

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeProcessor:
    timeline: list[str]
    fail_at_offset: int | None = None
    outcomes: dict[int, Outcome] = field(default_factory=dict)
    seen: list[Delivery] = field(default_factory=list)

    def process(self, delivery: Delivery) -> Outcome:
        self.seen.append(delivery)
        if delivery.offset == self.fail_at_offset:
            raise RuntimeError("the handover failed")
        self.timeline.append(f"processed {delivery.offset}")
        return self.outcomes.get(delivery.offset, Outcome.HANDED_OVER)


def run(
    messages: list[FakeMessage],
    fail_at_offset: int | None = None,
    outcomes: dict[int, Outcome] | None = None,
) -> dict[str, Any]:
    stop = threading.Event()
    timeline: list[str] = []
    kafka = FakeKafkaConsumer(messages, stop, timeline)
    processor = FakeProcessor(timeline, fail_at_offset, outcomes or {})
    PollLoop(kafka, "orders", processor, stop).run()  # type: ignore[arg-type]
    return {"timeline": timeline, "seen": processor.seen}


def test_each_offset_is_committed_after_its_message_was_handed_over() -> None:
    result = run([FakeMessage(7), FakeMessage(8)])

    assert result["timeline"] == ["processed 7", "commit 7", "processed 8", "commit 8"]


def test_a_duplicate_with_nothing_unfinished_is_committed() -> None:
    result = run([FakeMessage(7)], outcomes={7: Outcome.DUPLICATE})

    assert result["timeline"] == ["processed 7", "commit 7"]


def test_a_failure_while_processing_leaves_the_offset_uncommitted() -> None:
    timeline: list[str] = []
    with pytest.raises(RuntimeError):
        stop = threading.Event()
        kafka = FakeKafkaConsumer([FakeMessage(7), FakeMessage(8)], stop, timeline)
        PollLoop(kafka, "orders", FakeProcessor(timeline, fail_at_offset=8), stop).run()  # type: ignore[arg-type]

    assert timeline == ["processed 7", "commit 7"]
    assert kafka.closed, "the consumer must be closed even when processing fails"


def test_the_end_of_a_partition_is_not_a_message() -> None:
    end_of_partition = KafkaError(KafkaError._PARTITION_EOF)

    result = run([FakeMessage(3, error_=end_of_partition)])

    assert result["seen"] == []
    assert result["timeline"] == []
