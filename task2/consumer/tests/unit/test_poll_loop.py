"""The poll loop commits an offset only once its message reached a terminal outcome (ADR 0004),
pauses in place when processing asks it to stop (ADR 0006), stops after the current step when
shutdown is requested and drops an in-hand message whose partition moved (ADR 0007), and
processes each attempt at a message in a span of its own, continuing the record's trace
(ADR 0008)."""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from confluent_kafka import KafkaError, KafkaException, TopicPartition
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from structlog.testing import capture_logs

from order_consumer.adapters.inbound.poll_loop import PausedTooLong, PollLoop
from order_consumer.application.ports import Delivery, TraceSource
from order_consumer.application.processing import Interrupted, Outcome, Stopped, StopReason
from order_consumer.health import Beat

WRONG_TOKEN = Stopped(StopReason.FULFILMENT, "401 Unauthorized")
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
TRACEPARENT = f"00-{TRACE_ID}-b7ad6b7169203331-01"


@dataclass
class FakeMessage:
    offset_: int
    error_: KafkaError | None = None
    partition_: int = 0
    headers_: list[tuple[str, bytes]] | None = None

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
        return self.headers_

    def timestamp(self) -> tuple[int, int]:
        return (1, 1_792_228_324_000)


@dataclass
class FakeClock:
    now: float = 1000.0

    def monotonic(self) -> float:
        return self.now


# What one poll brings: a message, nothing, or something happening meanwhile, such as a rebalance
# (a callback, run inside poll) or a signal, followed by the message it returns, if any.
type Turn = FakeMessage | Callable[["FakeKafkaConsumer"], FakeMessage | None] | None


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
    strays_allowed: bool = False  # a record may arrive from a paused partition

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
            turn = turn(self)
        if turn is not None and turn.error() is None and not self.strays_allowed:
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

    def seek(self, partition: TopicPartition) -> None:
        self.timeline.append(f"seek {partition.partition}/{partition.offset}")

    def resume(self, partitions: list[TopicPartition]) -> None:
        numbers = sorted(tp.partition for tp in partitions)
        self.paused -= set(numbers)
        self.timeline.append(f"resume {numbers}")

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeProcessor:
    """Answers each delivery of an offset with the next of its scripted outcomes, then with the
    last one again; a handover by default. `stop_at` (an offset, and which of its deliveries,
    counted from 1) is a signal arriving mid-message."""

    timeline: list[str]
    clock: FakeClock
    stop: threading.Event
    fail_at_offset: int | None = None
    outcomes: dict[int, list[Outcome | Stopped | Interrupted]] = field(default_factory=dict)
    stop_at: tuple[int, int] | None = None
    seen: list[Delivery] = field(default_factory=list)

    def process(self, delivery: Delivery) -> Outcome | Stopped | Interrupted:
        self.seen.append(delivery)
        deliveries = sum(seen.offset == delivery.offset for seen in self.seen)
        if self.stop_at == (delivery.offset, deliveries):
            self.stop.set()
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
    outcomes: dict[int, list[Outcome | Stopped | Interrupted]] | None = None,
    stop_at: tuple[int, int] | None = None,
    probe_interval: float = 30.0,
    exit_after: float | None = None,
    processing_budget: float = 0.0,
) -> Run:
    stop = threading.Event()
    timeline: list[str] = []
    clock = FakeClock()
    kafka = FakeKafkaConsumer(turns, stop, timeline, clock)
    processor = FakeProcessor(timeline, clock, stop, fail_at_offset, outcomes or {}, stop_at)
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
        group="order-consumer",
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
        processor = FakeProcessor(timeline, clock, stop, fail_at_offset=8)
        PollLoop(  # type: ignore[arg-type]
            kafka, "orders", processor, stop, clock=clock, group="order-consumer"
        ).run()

    assert timeline == ["processed 7 at 1", "commit 7"]
    assert kafka.closed, "the consumer must be closed even when processing fails"


def test_the_end_of_a_partition_is_not_a_message() -> None:
    end_of_partition = KafkaError(KafkaError._PARTITION_EOF)

    result = run([FakeMessage(3, error_=end_of_partition)])

    assert result.seen == []
    assert result.timeline == []


def signal(message: FakeMessage | None = None) -> Turn:
    """SIGTERM arriving during a poll, which then returns `message`."""

    def arrive(kafka: FakeKafkaConsumer) -> FakeMessage | None:
        kafka.stop.set()
        return message

    return arrive


def test_a_stop_during_a_message_lets_it_finish_commits_it_and_closes() -> None:
    with capture_logs() as lines:
        result = run([FakeMessage(7), FakeMessage(8)], stop_at=(7, 1))

    assert result.timeline == ["processed 7 at 1", "commit 7"]
    assert result.kafka.turns == [FakeMessage(8)], "no further message is started"
    [stopping] = [line for line in lines if line["event"] == "consumer.stopping"]
    assert stopping["uncommitted"] is None


def test_a_message_whose_retries_the_stop_cut_short_stays_uncommitted() -> None:
    with capture_logs() as lines:
        result = run(
            [FakeMessage(7), FakeMessage(8)],
            outcomes={7: [Interrupted(attempts=1)]},
            stop_at=(7, 1),
        )

    assert result.timeline == ["processed 7 at 1"]
    [stopping] = [line for line in lines if line["event"] == "consumer.stopping"]
    assert stopping["uncommitted"] == "orders/0@7", "redelivered to the next owner"


def test_a_message_fetched_by_the_poll_the_stop_arrived_in_is_not_started() -> None:
    result = run([FakeMessage(7), signal(FakeMessage(8)), FakeMessage(9)])

    assert result.timeline == ["processed 7 at 1", "commit 7"]


def test_a_paused_instance_stops_at_its_next_turn_without_probing() -> None:
    """Paused at 1 s, a probe due at 31 s; the stop arrives at 11 s."""
    with capture_logs() as lines:
        result = run(
            [FakeMessage(7), *[None] * 9, signal(), *[None] * 30], outcomes={7: [WRONG_TOKEN]}
        )

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]"]
    assert len(result.kafka.turns) == 30
    [stopping] = [line for line in lines if line["event"] == "consumer.stopping"]
    assert stopping["uncommitted"] == "orders/0@7"
    assert stopping["paused"] == "systemic: fulfilment"
    assert not [line for line in lines if line["event"] == "message.dropped"], (
        "closing leaves the group: what was in hand is not dropped, only left uncommitted"
    )


def test_a_probe_the_stop_cut_short_leaves_the_message_uncommitted() -> None:
    with capture_logs() as lines:
        result = run(
            [FakeMessage(7), *[None] * 40],
            outcomes={7: [WRONG_TOKEN, Interrupted(attempts=1)]},
            stop_at=(7, 2),
        )

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]", "processed 7 at 31"]
    [stopping] = [line for line in lines if line["event"] == "consumer.stopping"]
    assert stopping["uncommitted"] == "orders/0@7"


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


def assign_all(kafka: FakeKafkaConsumer) -> None:
    """The first rebalance reporting the partitions the fake starts with."""
    kafka.assign_now([0, 1, 2])


def moved(callback: str, *partitions: int) -> Turn:
    """A rebalance taking these partitions away: revoked, or lost with the membership."""

    def rebalance(kafka: FakeKafkaConsumer) -> None:
        kafka.callbacks[callback](kafka, [TopicPartition("orders", p) for p in partitions])
        kafka.assigned = [p for p in kafka.assigned if p not in partitions]

    return rebalance


def test_a_message_in_hand_whose_partition_is_revoked_while_paused_is_dropped_uncommitted() -> None:
    """Its new owner processes it from the committed offset. The pause outlives the message: at
    the probe due at 32 s the remaining partitions resume, and the next message is the probe."""
    with capture_logs() as lines:
        result = run(
            [
                assign_all,
                FakeMessage(7),
                moved("on_revoke", 0),
                *[None] * 30,
                FakeMessage(9, partition_=1),
            ],
            outcomes={7: [WRONG_TOKEN]},
        )

    assert result.timeline == [
        "processed 7 at 2",
        "pause [0, 1, 2]",
        "resume [1, 2]",
        "processed 9 at 34",
        "commit 9",
    ]
    [dropped] = [line for line in lines if line["event"] == "message.dropped"]
    assert (dropped["topic"], dropped["partition"], dropped["offset"]) == ("orders", 0, 7)
    assert dropped["reason"] == "revoked"
    assert ("paused", ("orders/1", "orders/2"), "systemic: fulfilment") in result.states()
    assert result.states()[-1] == ("running", ("orders/1", "orders/2"), None)


def test_a_message_in_hand_stays_when_another_partition_is_revoked() -> None:
    with capture_logs() as lines:
        result = run(
            [FakeMessage(7), moved("on_revoke", 2), *[None] * 30], outcomes={7: [WRONG_TOKEN]}
        )

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]", "processed 7 at 31"]
    assert not [line for line in lines if line["event"] == "message.dropped"]


def test_a_message_in_hand_when_the_membership_is_lost_is_dropped_uncommitted() -> None:
    with capture_logs() as lines:
        result = run(
            [FakeMessage(7), moved("on_lost", 0, 1, 2), *[None] * 30],
            outcomes={7: [WRONG_TOKEN]},
        )

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]", "resume []"]
    [dropped] = [line for line in lines if line["event"] == "message.dropped"]
    assert (dropped["partition"], dropped["offset"], dropped["reason"]) == (0, 7, "lost")


def test_a_paused_instance_that_dropped_its_message_pauses_again_on_the_next_one() -> None:
    """The cause outlives the revocation: the next message meets it and pauses the instance
    again, holding that one."""
    result = run(
        [
            assign_all,
            FakeMessage(7),
            moved("on_revoke", 0),
            *[None] * 30,
            FakeMessage(9, partition_=1),
        ],
        outcomes={7: [WRONG_TOKEN], 9: [WRONG_TOKEN]},
    )

    assert result.timeline == [
        "processed 7 at 2",
        "pause [0, 1, 2]",
        "resume [1, 2]",
        "processed 9 at 34",
        "pause [1, 2]",
    ]
    assert result.states()[-1] == ("paused", ("orders/1", "orders/2"), "systemic: fulfilment")


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


def test_a_fatal_client_error_while_paused_is_raised() -> None:
    fatal = KafkaError(KafkaError._FATAL, fatal=True)

    with pytest.raises(KafkaException):
        run([FakeMessage(7), FakeMessage(0, error_=fatal)], outcomes={7: [WRONG_TOKEN]})


def test_a_record_reaching_a_paused_instance_is_put_back_unprocessed() -> None:
    """None should arrive; if one does, skipping it would let a later commit pass over it."""

    def allow_strays(kafka: FakeKafkaConsumer) -> None:
        kafka.strays_allowed = True

    result = run(
        [FakeMessage(7), allow_strays, FakeMessage(9, partition_=1)], outcomes={7: [WRONG_TOKEN]}
    )

    assert result.timeline == ["processed 7 at 1", "pause [0, 1, 2]", "seek 1/9", "pause [1]"]


def test_each_message_is_processed_in_a_span_continuing_its_trace(
    spans: InMemorySpanExporter,
) -> None:
    result = run([FakeMessage(7, headers_=[("traceparent", TRACEPARENT.encode())])])

    [span] = spans.get_finished_spans()
    assert span.name == "process orders"
    assert span.kind is trace.SpanKind.CONSUMER
    assert span.parent is not None and span.parent.span_id == 0xB7AD6B7169203331
    assert trace.format_trace_id(span.context.trace_id) == TRACE_ID
    assert dict(span.attributes or {}) == {
        "messaging.system": "kafka",
        "messaging.operation.type": "process",
        "messaging.operation.name": "process",
        "messaging.destination.name": "orders",
        "messaging.destination.partition.id": "0",
        "messaging.kafka.offset": 7,
        "messaging.consumer.group.name": "order-consumer",
        "outcome": "handed_over",
    }
    [delivery] = result.seen
    assert delivery.trace.trace_id == TRACE_ID
    assert delivery.trace.source is TraceSource.HEADER
    assert delivery.trace.traceparent == f"00-{TRACE_ID}-{span.context.span_id:016x}-01"


def test_a_trace_the_producer_did_not_sample_is_still_recorded(
    spans: InMemorySpanExporter,
) -> None:
    """Locally every order's trace is kept, whatever the producer decided (ADR 0008)."""
    unsampled = TRACEPARENT.removesuffix("01") + "00"

    result = run([FakeMessage(7, headers_=[("traceparent", unsampled.encode())])])

    [span] = spans.get_finished_spans()
    assert trace.format_trace_id(span.context.trace_id) == TRACE_ID
    [delivery] = result.seen
    assert delivery.trace.traceparent.endswith("-01"), "sampled from here on"


def test_a_message_without_a_traceparent_starts_a_new_trace(spans: InMemorySpanExporter) -> None:
    result = run([FakeMessage(7)])

    [span] = spans.get_finished_spans()
    assert span.parent is None
    [delivery] = result.seen
    assert delivery.trace.source is TraceSource.NEW
    assert delivery.trace.trace_id == trace.format_trace_id(span.context.trace_id)


def test_an_invalid_traceparent_is_replaced_by_a_new_trace(spans: InMemorySpanExporter) -> None:
    result = run([FakeMessage(7, headers_=[("traceparent", b"00-forged\n")])])

    [span] = spans.get_finished_spans()
    assert span.parent is None
    [delivery] = result.seen
    assert delivery.trace.source is TraceSource.REPLACED
    assert delivery.trace.trace_id == trace.format_trace_id(span.context.trace_id)


def test_a_failure_while_processing_ends_the_span_as_an_error(spans: InMemorySpanExporter) -> None:
    with pytest.raises(RuntimeError):
        run([FakeMessage(8)], fail_at_offset=8)

    [span] = spans.get_finished_spans()
    assert span.status.status_code is trace.StatusCode.ERROR
    assert "outcome" not in (span.attributes or {})


def test_every_probe_of_a_paused_message_is_a_span_of_its_own_in_the_same_trace(
    spans: InMemorySpanExporter,
) -> None:
    """Paused at 1 s, probed at 31 s with success: two process spans, both children of the
    record's traceparent."""
    message = FakeMessage(7, headers_=[("traceparent", TRACEPARENT.encode())])

    run([message, *[None] * 30], outcomes={7: [WRONG_TOKEN, Outcome.HANDED_OVER]})

    process_spans = [span for span in spans.get_finished_spans() if span.name == "process orders"]
    assert [dict(span.attributes or {})["outcome"] for span in process_spans] == [
        "stopped: systemic: fulfilment",
        "handed_over",
    ]
    assert {trace.format_trace_id(span.context.trace_id) for span in process_spans} == {TRACE_ID}
