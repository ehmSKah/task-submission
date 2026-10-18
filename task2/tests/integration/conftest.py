"""In-process consumers, each on its own topic and group (ADR 0009).

The stack's consumers never take a test's messages, and no test sees another's events.
"""

import threading
from collections.abc import Iterator
from uuid import uuid4

import pytest

from order_consumer import logs
from order_consumer.config import Settings
from order_consumer.main import build
from tests.integration.stack import (
    CONSUMER_DATABASE_URL,
    FULFILMENT_TOKEN,
    KAFKA_BOOTSTRAP,
    STUB_URL,
    ConsumerGroup,
    create_topic,
)
from tests.support.events import EVENT_SCHEMA


@pytest.fixture(scope="session", autouse=True)
def json_logs() -> None:
    logs.configure("order-consumer-test", "DEBUG")


@pytest.fixture
def topic() -> str:
    """The test's own topic, and its dead-letter topic named after it."""
    name = f"orders.test.{uuid4().hex[:12]}"
    create_topic(name)
    create_topic(f"{name}.dlt")
    return name


@pytest.fixture
def consumer(topic: str) -> Iterator[ConsumerGroup]:
    group = f"order-consumer-test-{uuid4().hex[:12]}"
    settings = Settings(
        kafka_bootstrap_servers=KAFKA_BOOTSTRAP,
        kafka_topic=topic,
        kafka_group_id=group,
        database_url=CONSUMER_DATABASE_URL,
        fulfilment_base_url=STUB_URL,
        fulfilment_token=FULFILMENT_TOKEN,
        kafka_dead_letter_topic=f"{topic}.dlt",
        event_schema=EVENT_SCHEMA,
    )
    stop = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            build(settings, stop).run()
        except BaseException as error:
            failures.append(error)
            raise

    thread = threading.Thread(target=run, name=f"consumer-{group}", daemon=True)
    thread.start()
    yield ConsumerGroup(topic=topic, group=group, dead_letter_topic=f"{topic}.dlt")
    stop.set()
    thread.join(timeout=15)
    assert not thread.is_alive(), "the in-process consumer did not stop"
    assert not failures, f"the in-process consumer failed: {failures[0]!r}"
