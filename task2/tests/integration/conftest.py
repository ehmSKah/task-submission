"""In-process consumers, each on its own topic and group (ADR 0009).

The stack's consumers never take a test's messages, and no test sees another's events.
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest

from order_consumer import logs
from order_consumer.config import Settings
from tests.integration.in_process import group_of, running
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
def consumer_settings(topic: str) -> Settings:
    """One group per test; every consumer instance a test starts with these settings joins it."""
    return Settings(
        kafka_bootstrap_servers=KAFKA_BOOTSTRAP,
        kafka_topic=topic,
        kafka_group_id=f"order-consumer-test-{uuid4().hex[:12]}",
        database_url=CONSUMER_DATABASE_URL,
        fulfilment_base_url=STUB_URL,
        fulfilment_token=FULFILMENT_TOKEN,
        kafka_dead_letter_topic=f"{topic}.dlt",
        event_schema=EVENT_SCHEMA,
    )


@pytest.fixture
def consumer(consumer_settings: Settings) -> Iterator[ConsumerGroup]:
    with running(consumer_settings):
        yield group_of(consumer_settings)
