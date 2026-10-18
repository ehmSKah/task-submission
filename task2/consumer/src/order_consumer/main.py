"""The consumer's entry point: settings, logging, wiring, signals."""

import random
import signal
import socket
import threading
from collections.abc import Callable

import structlog

from order_consumer import logs, tracing
from order_consumer.adapters.inbound.poll_loop import PausedTooLong, PollLoop, create_consumer
from order_consumer.adapters.outbound.fulfilment_http import HttpFulfilmentClient
from order_consumer.adapters.outbound.kafka_dead_letters import (
    FLUSH_TIMEOUT_S,
    KafkaDeadLetterSink,
    create_producer,
)
from order_consumer.adapters.outbound.postgres_store import PostgresOrderStore
from order_consumer.adapters.outbound.system_clock import SystemClock
from order_consumer.application.ports import FulfilmentClient, OrderStore
from order_consumer.application.processing import OrderProcessor
from order_consumer.application.retries import Jitter, full_jitter
from order_consumer.config import ConfigurationError, Settings
from order_consumer.domain.event import OrderPlacedReader
from order_consumer.health import HeartbeatFile

log = structlog.get_logger()

EXIT_CLEAN = 0
EXIT_CRASH = 1
EXIT_CONFIGURATION = 2
EXIT_PAUSED_TOO_LONG = 3


def _as_is[T](adapter: T) -> T:
    return adapter


def build(
    settings: Settings,
    stop: threading.Event,
    wrap_store: Callable[[OrderStore], OrderStore] = _as_is,
    wrap_fulfilment: Callable[[FulfilmentClient], FulfilmentClient] = _as_is,
    jitter: Jitter | None = None,
) -> PollLoop:
    """Wires the real adapters; the in-process integration tests use this too, wrapping adapters
    to simulate a crash between two steps (ADR 0007), or pinning the jitter where a scenario
    depends on when attempts happen (ADR 0009)."""
    store = PostgresOrderStore(settings.database_url)
    fulfilment = HttpFulfilmentClient(
        settings.fulfilment_base_url, settings.fulfilment_token, settings.fulfilment_timeouts
    )
    dead_letters = KafkaDeadLetterSink(
        create_producer(settings),
        settings.kafka_dead_letter_topic,
        consumer=f"{settings.kafka_group_id}/{socket.gethostname()}",
    )
    reader = OrderPlacedReader.from_file(settings.event_schema)
    clock = SystemClock()
    exit_after = settings.pause_exit_after_minutes
    return PollLoop(
        create_consumer(settings),
        settings.kafka_topic,
        OrderProcessor(
            wrap_store(store),
            wrap_fulfilment(fulfilment),
            dead_letters,
            reader,
            policy=settings.retry_policy,
            clock=clock,
            jitter=jitter or full_jitter(random.Random()),
            thresholds=settings.flood_guard,
        ),
        stop,
        clock=clock,
        probe_interval=settings.pause_probe_interval_seconds,
        exit_after=None if exit_after is None else exit_after * 60,
        heartbeat=HeartbeatFile(settings.health_file).write,
        # A handover's retries at their longest, then waiting for its dead letter's confirmation.
        processing_budget=settings.retry_policy.worst_case_seconds(
            settings.fulfilment_timeouts.attempt_deadline
        )
        + FLUSH_TIMEOUT_S,
        on_close=(store.close, fulfilment.close, dead_letters.close),
        group=settings.kafka_group_id,
    )


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        logs.configure("order-consumer")
        log.error("consumer.configuration_invalid", reason=str(error))
        return EXIT_CONFIGURATION
    logs.configure("order-consumer", settings.log_level)
    tracer_provider = tracing.configure("order-consumer", settings.otlp_endpoint)

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    try:
        build(settings, stop).run()
    except PausedTooLong as paused:
        # Nothing was committed: the message in hand waits for the instance that takes over.
        log.error("consumer.stopped", exit_code=EXIT_PAUSED_TOO_LONG, reason=str(paused))
        return EXIT_PAUSED_TOO_LONG
    except Exception as crash:
        # Without a stack trace: only processing.unexpected_error carries one (ADR 0008).
        log.error(
            "consumer.stopped", exit_code=EXIT_CRASH, reason=f"{type(crash).__name__}: {crash}"
        )
        return EXIT_CRASH
    finally:
        tracer_provider.shutdown()  # exports the spans still waiting in their batch
    log.info("consumer.stopped", exit_code=EXIT_CLEAN)
    return EXIT_CLEAN
