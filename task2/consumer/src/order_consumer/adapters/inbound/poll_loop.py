"""The poll loop: the only place that commits offsets (ADR 0004, 0005).

One message at a time. An offset is committed synchronously, and only once its message has reached
a terminal outcome, so anything unfinished is redelivered after a crash. When processing asks to
stop, the loop pauses in place: its partitions are paused, it keeps polling so it stays in the
group, and it tries the message in hand again every probe interval (ADR 0006). When shutdown is
requested it finishes the current step, starts nothing more and leaves the group; an in-hand
message whose partition moves to another instance is dropped, uncommitted (ADR 0007). Each attempt
at a message, a probe included, is processed in a span of its own, continuing the trace its record
carries (ADR 0008).
"""

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from confluent_kafka import (
    TIMESTAMP_NOT_AVAILABLE,
    Consumer,
    KafkaError,
    KafkaException,
    Message,
    TopicPartition,
)
from opentelemetry import trace

from order_consumer import tracing
from order_consumer.application.ports import Clock, Delivery, MessageTrace
from order_consumer.application.processing import Interrupted, OrderProcessor, Outcome, Stopped
from order_consumer.config import Settings
from order_consumer.health import Beat

log = structlog.get_logger()
tracer = trace.get_tracer(__name__)

# How long one loop turn may take: poll() returns within a second, even while paused.
TURN_GRACE_SECONDS = 30.0
# A member that joins is first given nothing, and its partitions in the cooperative protocol's
# second round a moment later; only an empty assignment lasting this long means it is idle.
IDLE_AFTER_SECONDS = 5.0


def create_consumer(settings: Settings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            # Offsets follow outcomes, never the client's defaults (ADR 0004).
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "earliest",
            # A handover's retries keep the message in hand without polling; the start-up check
            # keeps them within half of this (ADR 0006).
            "max.poll.interval.ms": settings.kafka_max_poll_interval_ms,
            "partition.assignment.strategy": "cooperative-sticky",
            "logger": logging.getLogger("librdkafka"),
        }
    )


def _delivery(
    message: Message,
    topic: str,
    headers: tuple[tuple[str, bytes | None], ...],
    message_trace: MessageTrace,
) -> Delivery:
    timestamp_type, timestamp_ms = message.timestamp()
    return Delivery(
        key=message.key(),
        value=message.value(),
        topic=topic,
        partition=message.partition() or 0,
        offset=message.offset() or 0,
        headers=headers,
        timestamp=None
        if timestamp_type == TIMESTAMP_NOT_AVAILABLE
        else datetime.fromtimestamp(timestamp_ms / 1000, UTC),
        trace=message_trace,
    )


def _headers(message: Message) -> tuple[tuple[str, bytes | None], ...]:
    """Kept in their order and with repeated keys. The client's types also allow str values,
    though a consumed record's are bytes."""
    headers = message.headers() or []
    pairs = headers.items() if isinstance(headers, dict) else headers
    return tuple(
        (name, value.encode() if isinstance(value, str) else value) for name, value in pairs
    )


def _partition_labels(partitions: Iterable[TopicPartition]) -> list[str]:
    return [f"{partition.topic}/{partition.partition}" for partition in partitions]


def _partition_of(message: Message, topic: str) -> TopicPartition:
    return TopicPartition(message.topic() or topic, message.partition() or 0)


def _message_label(message: Message, topic: str) -> str:
    """Where a message was read, e.g. "orders/0@7"."""
    return f"{message.topic() or topic}/{message.partition() or 0}@{message.offset() or 0}"


class PausedTooLong(Exception):
    """Still paused after the optional limit: exiting hands the partitions to another instance,
    which helps when the cause is local to this one (ADR 0006)."""


@dataclass
class _Paused:
    """The message in hand while consumption is stopped, and why it stopped. None once it was
    dropped because its partition moved: the pause outlives it (ADR 0007)."""

    message: Message | None
    stopped: Stopped
    since: float  # monotonic
    next_probe: float  # monotonic


class PollLoop:
    def __init__(
        self,
        consumer: Consumer,
        topic: str,
        processor: OrderProcessor,
        stop: threading.Event,
        *,
        clock: Clock,
        probe_interval: float = 30.0,
        exit_after: float | None = None,  # seconds paused before giving up; None: never
        heartbeat: Callable[[Beat], None] = lambda _beat: None,
        processing_budget: float = 0.0,  # the longest processing one message may take
        on_close: Iterable[Callable[[], None]] = (),
        group: str,
    ) -> None:
        self._consumer = consumer
        self._topic = topic
        self._group = group
        self._processor = processor
        self._stop = stop
        self._clock = clock
        self._probe_interval = probe_interval
        self._exit_after = exit_after
        self._heartbeat = heartbeat
        self._processing_budget = processing_budget
        self._on_close = list(on_close)
        self._paused: _Paused | None = None
        self._interrupted: Message | None = None  # cut short by the stop, left uncommitted
        self._partitions: set[str] | None = None  # e.g. "orders/0"; None until the first assignment
        self._assigned_at = 0.0  # monotonic, the latest assignment
        self._started = False  # has held partitions, or been knowingly idle

    def run(self) -> None:
        self._beat()
        self._consumer.subscribe(
            [self._topic],
            on_assign=self._assigned,
            on_revoke=self._revoked,
            on_lost=self._lost,
        )
        log.info("consumer.started", topic=self._topic)
        try:
            while not self._stop.is_set():
                self._beat()
                message = self._consumer.poll(1.0)
                error = None if message is None else message.error()
                if error is not None:
                    self._client_error(error)
                elif self._stop.is_set():
                    break  # a message fetched meanwhile is left to the next owner
                elif self._paused is not None:
                    if message is not None:
                        self._hold_back(message)
                    self._probe_if_due(self._paused)
                elif message is not None:
                    self._handle(message)
            self._log_stopping()
        finally:
            # What is in hand stays uncommitted; leaving the group is not dropping it.
            self._paused = None
            self._consumer.close()
            for close in self._on_close:
                close()

    def _log_stopping(self) -> None:
        """The step in progress is finished; anything not terminal stays uncommitted and is
        redelivered to the instance that takes over (ADR 0007)."""
        paused = self._paused
        in_hand = paused.message if paused is not None else self._interrupted
        log.info(
            "consumer.stopping",
            paused=None if paused is None else paused.stopped.reason.value,
            uncommitted=None if in_hand is None else _message_label(in_hand, self._topic),
        )

    @staticmethod
    def _client_error(error: KafkaError) -> None:
        if error.fatal():
            raise KafkaException(error)
        if error.code() != KafkaError._PARTITION_EOF:
            log.warning("kafka.error", reason=error.str())

    def _handle(self, message: Message) -> None:
        stopped = self._process(message)
        if isinstance(stopped, Interrupted):
            self._interrupted = message
        elif stopped is not None:
            now = self._clock.monotonic()
            self._consumer.pause(self._consumer.assignment())
            self._paused = _Paused(message, stopped, now, now + self._probe_interval)
            self._log_paused(stopped)

    def _process(self, message: Message) -> Stopped | Interrupted | None:
        """Commits once the message reached a terminal outcome; otherwise says why it stopped, or
        that the stop cut it short. In the message's span, named and described by OpenTelemetry's
        messaging conventions."""
        self._beat(busy=True)
        topic = message.topic() or self._topic
        headers = _headers(message)
        parent, source = tracing.incoming_trace(headers)
        with tracer.start_as_current_span(
            f"process {topic}",
            context=parent,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "kafka",
                "messaging.operation.type": "process",
                "messaging.operation.name": "process",
                "messaging.destination.name": topic,
                "messaging.destination.partition.id": str(message.partition() or 0),
                "messaging.kafka.offset": message.offset() or 0,
                "messaging.consumer.group.name": self._group,
            },
        ) as span:
            delivery = _delivery(message, topic, headers, tracing.current_trace(source))
            match self._processor.process(delivery):
                case Outcome() as outcome:
                    span.set_attribute("outcome", outcome.value)
                    self._commit(message)
                    return None
                case Stopped() as stopped:
                    span.set_attribute("outcome", f"stopped: {stopped.reason.value}")
                    return stopped
                case Interrupted() as interrupted:
                    span.set_attribute("outcome", "interrupted")
                    return interrupted

    def _commit(self, message: Message) -> None:
        """A commit refused because the group moved on (a zombie whose partition went to another
        instance while it was busy) is logged and skipped: the new owner processes the message
        from the committed offset, which is harmless (ADR 0004, 0007)."""
        try:
            self._consumer.commit(message=message, asynchronous=False)
        except KafkaException as failure:
            error: KafkaError = failure.args[0]
            if error.fatal():
                raise
            log.error(
                "commit.failed",
                topic=message.topic() or self._topic,
                partition=message.partition() or 0,
                offset=message.offset() or 0,
                reason=f"{error.name()}: {error.str()}",
            )

    @staticmethod
    def _log_paused(stopped: Stopped) -> None:
        log.warning("consumer.paused", reason=stopped.reason.value, error=stopped.error)

    def _hold_back(self, message: Message) -> None:
        """A record reaching a paused instance (none should) is put back and its partition paused:
        skipping it would let a later commit pass over it."""
        partition = TopicPartition(message.topic() or self._topic, message.partition() or 0)
        self._consumer.seek(
            TopicPartition(partition.topic, partition.partition, message.offset() or 0)
        )
        self._consumer.pause([partition])

    def _probe_if_due(self, paused: _Paused) -> None:
        """The message in hand is tried again, fully: processing is idempotent (ADR 0004)."""
        now = self._clock.monotonic()
        if self._exit_after is not None and now - paused.since >= self._exit_after:
            raise PausedTooLong(
                f"paused for {now - paused.since:.0f} s ({paused.stopped.reason.value}): "
                f"{paused.stopped.error}"
            )
        if now < paused.next_probe:
            return
        if paused.message is None:
            # Nothing left to probe with: the next message from the remaining partitions is the
            # probe, and pauses the instance again if the cause is still there.
            self._consumer.resume(self._consumer.assignment())
            self._paused = None
            log.info("consumer.resumed", reason=paused.stopped.reason.value)
            return
        stopped = self._process(paused.message)
        if stopped is None:
            self._consumer.resume(self._consumer.assignment())
            self._paused = None
            log.info("consumer.resumed", reason=paused.stopped.reason.value)
            return
        if isinstance(stopped, Interrupted):
            return  # still paused, with the message in hand: the loop is stopping
        if stopped.reason is not paused.stopped.reason:
            self._log_paused(stopped)
        paused.stopped = stopped
        paused.next_probe = self._clock.monotonic() + self._probe_interval

    def _beat(self, busy: bool = False) -> None:
        """A busy loop doesn't poll until the message is processed: the beat says how long that
        may take, so it isn't mistaken for a stuck one (ADR 0007)."""
        partitions = tuple(sorted(self._partitions or ()))
        due_within = TURN_GRACE_SECONDS + (self._processing_budget if busy else 0.0)
        self._started = self._started or (
            self._partitions is not None
            and (
                bool(self._partitions)
                or self._clock.monotonic() - self._assigned_at >= IDLE_AFTER_SECONDS
            )
        )
        if not self._started:
            beat = Beat("starting", partitions, due_within)
        elif self._paused is not None:
            stopped = self._paused.stopped
            beat = Beat("paused", partitions, due_within, stopped.reason.value, stopped.error)
        else:
            beat = Beat("running", partitions, due_within)
        self._heartbeat(beat)

    def _assigned(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        labels = _partition_labels(partitions)
        log.info("partitions.assigned", partitions=labels)
        self._partitions = (self._partitions or set()) | set(labels)
        self._assigned_at = self._clock.monotonic()
        if self._paused is not None and partitions:
            # Assigned here rather than after the callback, so they can be paused before the
            # first fetch: a paused instance consumes nothing, whatever it is given.
            self._consumer.incremental_assign(partitions)
            self._consumer.pause(partitions)

    def _revoked(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.info("partitions.revoked", partitions=self._forget(partitions, "revoked"))

    def _lost(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        log.warning("partitions.lost", partitions=self._forget(partitions, "lost"))

    def _forget(self, partitions: list[TopicPartition], why: str) -> list[str]:
        """Never commits: every finished message is committed already, and only a paused instance
        holds one that isn't. If its partition moved, it is dropped; the new owner processes it
        from the committed offset (ADR 0007)."""
        labels = _partition_labels(partitions)
        self._partitions = (self._partitions or set()) - set(labels)
        paused = self._paused
        if paused is not None and paused.message is not None:
            in_hand = paused.message
            [in_hand_partition] = _partition_labels([_partition_of(in_hand, self._topic)])
            if in_hand_partition in labels:
                log.warning(
                    "message.dropped",
                    topic=in_hand.topic() or self._topic,
                    partition=in_hand.partition() or 0,
                    offset=in_hand.offset() or 0,
                    reason=why,
                )
                paused.message = None
        return labels
