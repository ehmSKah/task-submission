"""The consumer's entry point: settings, logging, wiring, signals."""

import signal
import threading

import structlog

from order_consumer import logs
from order_consumer.adapters.inbound.poll_loop import PollLoop, create_consumer
from order_consumer.adapters.outbound.fulfilment_http import HttpFulfilmentClient
from order_consumer.adapters.outbound.postgres_store import PostgresOrderStore
from order_consumer.application.processing import OrderProcessor
from order_consumer.config import ConfigurationError, Settings

log = structlog.get_logger()

EXIT_CLEAN = 0
EXIT_CONFIGURATION = 2


def build(settings: Settings, stop: threading.Event) -> PollLoop:
    """Wires the real adapters; the in-process integration tests use this too."""
    store = PostgresOrderStore(settings.database_url)
    fulfilment = HttpFulfilmentClient(settings.fulfilment_base_url, settings.fulfilment_token)
    return PollLoop(
        create_consumer(settings),
        settings.kafka_topic,
        OrderProcessor(store, fulfilment),
        stop,
        on_close=(store.close, fulfilment.close),
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
