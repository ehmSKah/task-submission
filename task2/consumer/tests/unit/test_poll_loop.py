"""The poll loop commits an offset only once its message reached a terminal outcome (ADR 0004),
and pauses in place when processing asks it to stop (ADR 0006)."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from confluent_kafka import KafkaError, TopicPartition

from order_consumer.adapters.inbound.poll_loop import PausedTooLong, PollLoop
from order_consumer.application.ports import Delivery
from order_consumer.application.processing import Outcome, Stopped
from order_consumer.health import Beat

WRONG_TOKEN = Stopped("systemic: fulfilment", "401 Unauthorized")


@dataclass
class FakeMessage:
    offset_: int
    error_: KafkaError | None = None
    partition_: int = 0

    def error(self) -> KafkaError | None:
        return self.error_

    def key(self) -> bytes:
        return b"order"

    def value(self) -> bytes:
        return b"{}"

    def topic(self) -> str:
        return "orders"

    def partition(self) -> int:
        return self.partition_

    def offset(self) -> int:
        return self.offset_

    def headers(self) -> list[tuple[str, bytes]] | None:
        return None

    def timestamp(self) -> tuple[int, int]:
        return (1, 1_792_228_324_000)


@dataclass
class FakeClock:
    now: float = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# What one poll brings: a message, nothing, or a rebalance (a callback, run inside poll).
type Turn = FakeMessage | Callable[["FakeKafkaConsumer"], None] | None


@dataclass
class FakeKafkaConsumer:
    """Plays its turns once, one per poll, each poll taking its full timeout; then asks the loop to
    stop. Starts with partitions 0 to 2 of `orders` assigned."""

    turns: list[Turn]
    stop: threading.Event
    timeline: list[str]
    clock: FakeClock
    closed: bool = False
    assigned: list[int] = field(default_factory=lambda: [0, 1, 2])
    paused: set[int] = field(default_factory=set)
    callbacks: dict[str, Any] = field(default_factory=dict)

    def subscribe(self, topics: list[str], **callbacks: Any) -> None:
        self.callbacks = callbacks

    def assign_now(self, partitions: list[int]) -> None:
        """A rebalance assigning these partitions, as the client reports it to the callback."""
        self.callbacks["on_assign"](self, [TopicPartition("orders", p) for p in partitions])
        self.assigned = sorted({*self.assigned, *partitions})

    def poll(self, timeout: float) -> FakeMessage | None:
        if not self.turns:
            self.stop.set()
            return None
        self.clock.now += timeout
        turn = self.turns.pop(0)
        if callable(turn):
            turn(self)
            return None
        if turn is not None:
            assert turn.partition() not in self.paused, "a paused partition returned a message"
        return turn

    def commit(self, message: FakeMessage, asynchronous: bool) -> None:
        assert asynchronous is False, "commits must be synchronous"
        self.timeline.append(f"commit {message.offset()}")

    def assignment(self) -> list[TopicPartition]:
        return [TopicPartition("orders", partition) for partition in self.assigned]

    def incremental_assign(self, partitions: list[TopicPartition]) -> None:
        self.assigned = sorted({*self.assigned, *(tp.partition for tp in partitions)})

    def pause(self, partitions: list[TopicPartition]) -> None:
        numbers = sorted(tp.partition for tp in partitions)
        self.paused |= set(numbers)
        self.timeline.append(f"pause {numbers}")

    def resume(self, partitions: list[TopicPartition]) -> None:
        numbers = sorted(tp.partition for tp in partitions)
        self.paused -= set(numbers)
        self.timeline.append(f"resume {numbers}")

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeProcessor:
    """Answers each delivery of an offset with the next of its scripted outcomes, then with the
    last one again; a handover by default."""

    timeline: list[str]
    clock: FakeClock
    fail_at_offset: int | None = None
    outcomes: dict[int, list[Outcome | Stopped]] = field(default_factory=dict)
    seen: list[Delivery] = field(default_factory=list)

    def process(self, delivery: Delivery) -> Outcome | Stopped:
        self.seen.append(delivery)
        if delivery.offset == self.fail_at_offset:
            raise RuntimeError("a crash")
        self.timeline.append(f"processed {delivery.offset} at {self.clock.now - 1000:g}")
        script = self.outcomes.get(delivery.offset, [Outcome.HANDED_OVER])
        return script.pop(0) if len(script) > 1 else script[0]


@dataclass
class Run:
    timeline: list[str]
    seen: list[Delivery]
    kafka: FakeKafkaConsumer
    beats: list[Beat]

    def states(self) -> list[tuple[str, tuple[str, ...], str | None]]:
        """Each change of state, partitions or reason the heartbeat reported."""
        changes: list[tuple[str, tuple[str, ...], str | None]] = []
        for beat in self.beats:
            change = (beat.state, beat.partitions, beat.reason)
            if not changes or changes[-1] != change:
                changes.append(change)
        return changes


def run(
    turns: list[Turn],
    fail_at_offset: int | None = None,
    outcomes: dict[int, list[Outcome | Stopped]] | None = None,
    probe_interval: float = 30.0,
    exit_after: float | None = None,
    processing_budget: float = 0.0,
) -> Run:
    stop = threading.Event()
    timeline: list[str] = []
    clock = FakeClock()
    kafka = FakeKafkaConsumer(turns, stop, timeline, clock)
    processor = FakeProcessor(timeline, clock, fail_at_offset, outcomes or {})
    beats: list[Beat] = []
    loop = PollLoop(
        kafka,  # type: ignore[arg-type]
        "orders",
        processor,  # type: ignore[arg-type]
        stop,
        clock=clock,
        probe_interval=probe_interval,
        exit_after=exit_after,
        heartbeat=beats.append,
        processing_budget=processing_budget,
    )
    try:
        loop.run()
    finally:
        assert kafka.closed, "the consumer must be closed however the loop ends"
    return Run(timeline, processor.seen, kafka, beats)


def test_each_offset_is_committed_after_its_message_was_handed_over() -> None:
    result = run([FakeMessage(7), FakeMessage(8)])

    assert result.timeline == ["processed 7 at 1", "commit 7", "processed 8 at 2", "commit 8"]


def test_a_duplicate_with_nothing_unfinished_is_committed() -> None:
    result = run([FakeMessage(7)], outcomes={7: [Outcome.DUPLICATE]})

    assert result.timeline == ["processed 7 at 1", "commit 7"]


def test_the_offset_of_a_dead_lettered_message_is_committed() -> None:
    result = run([FakeMessage(7)], outcomes={7: [Outcome.DEAD_LETTERED]})

    assert result.timeline == ["processed 7 at 1", "commit 7"]


def test_a_crash_while_processing_leaves_the_offset_uncommitted() -> None:
    timeline: list[str] = []
    with pytest.raises(RuntimeError):
        stop = threading.Event()
        clock = FakeClock()
        kafka = FakeKafkaConsumer([FakeMessage(7), FakeMessage(8)], stop, timeline, clock)
        processor = FakeProcessor(timeline, clock, fail_at_offset=8)
        PollLoop(kafka, "orders", processor, stop, clock=clock).run()  # type: ignore[arg-type]

    assert timeline == ["processed 7 at 1", "commit 7"]
    assert kafka.closed, "the consumer must be closed even when processing fails"


def test_the_end_of_a_partition_is_not_a_message() -> None:
    end_of_partition = KafkaError(KafkaError._PARTITION_EOF)

    result = run([FakeMessage(3, error_=end_of_partition)])

    assert result.seen == []
    assert result.timeline == []


def test_a_stopped_outcome_pauses_every_partition_without_committing_and_keeps_polling() -> None:
    result = run([FakeMessage(7), None, None, None], outcomes={7: [WRONG_TOKEN]})

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]"]
    assert result.kafka.turns == [], "it went on polling while paused"


def test_the_message_in_hand_is_probed_every_interval_and_success_resumes_in_order() -> None:
    """Paused at 1 s; probes at 31 and 61 s, the second succeeds: the offset is committed, the
    partitions resume, and the next message follows."""
    quiet = [None] * 60
    result = run(
        [FakeMessage(7), *quiet, FakeMessage(8)],
        outcomes={7: [WRONG_TOKEN, WRONG_TOKEN, Outcome.HANDED_OVER]},
    )

    assert result.timeline == [
        "processed 7 at 1",
        "pause [0, 1, 2]",
        "processed 7 at 31",
        "processed 7 at 61",
        "commit 7",
        "resume [0, 1, 2]",
        "processed 8 at 62",
        "commit 8",
    ]


def test_partitions_assigned_while_paused_are_paused_too() -> None:
    """With the cooperative protocol, a rebalance adds partitions without touching the paused
    ones; without this, the new ones would be consumed while the instance is meant to stop."""

    def rebalance(kafka: FakeKafkaConsumer) -> None:
        kafka.assigned = [0]
        kafka.assign_now([3])

    result = run([FakeMessage(7), rebalance, None], outcomes={7: [WRONG_TOKEN]})

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]", "pause [3]"]
    assert 3 in result.kafka.assigned


def test_partitions_assigned_while_running_are_consumed() -> None:
    result = run([lambda kafka: kafka.assign_now([3]), FakeMessage(7, partition_=3)])

    assert result.timeline == ["processed 7 at 2", "commit 7"]


def test_an_instance_paused_longer_than_the_limit_gives_up_without_committing() -> None:
    """Exiting hands its partitions to another instance (ADR 0006, exit code 3 in ADR 0007)."""
    turns: list[Turn] = [FakeMessage(7), *[None] * 200]

    with pytest.raises(PausedTooLong, match="systemic: fulfilment"):
        run(turns, outcomes={7: [WRONG_TOKEN]}, exit_after=90.0)

    assert len(turns) == 200 - 90, "it gave up 90 s after pausing"


def test_without_a_limit_a_paused_instance_never_gives_up() -> None:
    result = run([FakeMessage(7), *[None] * 1000], outcomes={7: [WRONG_TOKEN]})

    assert result.kafka.turns == []
    assert result.timeline.count("processed 7 at 1") == 1
    assert len(result.timeline) == 2 + 1000 // 30


def test_the_heartbeat_reports_starting_then_the_partitions_then_a_pause_and_its_reason() -> None:
    def assign(kafka: FakeKafkaConsumer) -> None:
        kafka.assign_now([0, 2])

    result = run(
        [None, assign, FakeMessage(7), *[None] * 30],
        outcomes={7: [WRONG_TOKEN, Outcome.HANDED_OVER]},
    )

    assert result.states() == [
        ("starting", (), None),
        ("running", ("orders/0", "orders/2"), None),
        ("paused", ("orders/0", "orders/2"), "systemic: fulfilment"),
        ("running", ("orders/0", "orders/2"), None),
    ]
    paused = next(beat for beat in result.beats if beat.state == "paused")
    assert paused.error == "401 Unauthorized"


def test_an_instance_left_without_partitions_is_knowingly_idle_once_the_rebalance_settled() -> None:
    """More members than partitions: the rebalance gave this one nothing. A member that joins
    is first given nothing and its partitions a moment later (the cooperative protocol's second
    round), so nothing counts as idle only once it has lasted 5 s."""
    result = run([lambda kafka: kafka.assign_now([]), *[None] * 6])

    assert result.states() == [("starting", (), None), ("running", (), None)]
    first_running = next(i for i, beat in enumerate(result.beats) if beat.state == "running")
    assert first_running == 1 + 5 + 1, "running from the beat 5 s after the empty assignment"


def test_a_member_given_nothing_then_its_partitions_goes_straight_to_running() -> None:
    def second_round(kafka: FakeKafkaConsumer) -> None:
        kafka.assign_now([1])

    result = run([lambda kafka: kafka.assign_now([]), second_round, None])

    assert result.states() == [("starting", (), None), ("running", ("orders/1",), None)]


def test_an_instance_that_held_partitions_and_lost_them_all_stays_running() -> None:
    def revoke_all(kafka: FakeKafkaConsumer) -> None:
        kafka.callbacks["on_revoke"](kafka, [TopicPartition("orders", 0)])

    result = run([lambda kafka: kafka.assign_now([0]), revoke_all, None])

    assert result.states()[1:] == [("running", ("orders/0",), None), ("running", (), None)]


def test_revoked_partitions_leave_the_heartbeat() -> None:
    def revoke(kafka: FakeKafkaConsumer) -> None:
        kafka.callbacks["on_revoke"](kafka, [TopicPartition("orders", 0)])

    result = run([lambda kafka: kafka.assign_now([0, 1]), revoke, None])

    assert result.states()[-1] == ("running", ("orders/1",), None)


def test_the_heartbeat_is_written_on_every_turn_and_allows_for_processing() -> None:
    """A message's retries keep the loop from polling; the beat before it says so, so a busy loop
    isn't mistaken for a stuck one (ADR 0007)."""
    result = run([None, FakeMessage(7), None], processing_budget=110.0)

    assert [beat.due_within for beat in result.beats] == [30.0, 30.0, 30.0, 140.0, 30.0, 30.0]
