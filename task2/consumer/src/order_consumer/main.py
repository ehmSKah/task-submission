"""The consumer's entry point: settings, logging, wiring, signals."""

import signal
import socket
import threading

import structlog

from order_consumer import logs
from order_consumer.adapters.inbound.poll_loop import PollLoop, create_consumer
from order_consumer.adapters.outbound.fulfilment_http import HttpFulfilmentClient
from order_consumer.adapters.outbound.kafka_dead_letters import KafkaDeadLetterSink, create_producer
from order_consumer.adapters.outbound.postgres_store import PostgresOrderStore
from order_consumer.application.processing import OrderProcessor
from order_consumer.config import ConfigurationError, Settings
from order_consumer.domain.event import OrderPlacedReader

log = structlog.get_logger()

EXIT_CLEAN = 0
EXIT_CONFIGURATION = 2


def build(settings: Settings, stop: threading.Event) -> PollLoop:
    """Wires the real adapters; the in-process integration tests use this too."""
    store = PostgresOrderStore(settings.database_url)
    fulfilment = HttpFulfilmentClient(settings.fulfilment_base_url, settings.fulfilment_token)
    dead_letters = KafkaDeadLetterSink(
        create_producer(settings),
        settings.kafka_dead_letter_topic,
        consumer=f"{settings.kafka_group_id}/{socket.gethostname()}",
    )
    reader = OrderPlacedReader.from_file(settings.event_schema)
    return PollLoop(
        create_consumer(settings),
        settings.kafka_topic,
        OrderProcessor(store, fulfilment, dead_letters, reader),
        stop,
        on_close=(store.close, fulfilment.close, dead_letters.close),
    )


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigurationError as error:
        logs.configure("order-consumer")
        log.error("consumer.configuration_invalid", reason=str(error))
        return EXIT_CONFIGURATION
    logs.configure("order-consumer", settings.log_level)

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    build(settings, stop).run()
    log.info("consumer.stopped", exit_code=EXIT_CLEAN)
    return EXIT_CLEAN
