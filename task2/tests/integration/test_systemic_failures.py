"""Systemic failures beyond the fulfilment service (ADR 0006, 0007): the dead-letter topic can't be
written, and an instance paused longer than its optional limit exits with code 3.

A wrong token is in the downstream-failures file; PostgreSQL going down is proven on the stack's
own consumers, in the reliability suite.
"""

import dataclasses
import os
import signal
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from order_consumer.config import Settings
from order_consumer.main import EXIT_PAUSED_TOO_LONG, main
from tests.integration.in_process import group_of, heartbeat, running
from tests.integration.stack import (
    CONSUMER_DATABASE_URL,
    KAFKA_BOOTSTRAP,
    STUB_URL,
    assert_all_committed,
    create_topic,
    eventually,
    lag,
    publish,
    publish_records,
    records,
    wait_for_dead_letters,
)
from tests.support.events import EVENT_SCHEMA, order_placed_event

pytestmark = pytest.mark.integration


def test_an_unwritable_dead_letter_topic_pauses_the_consumer_until_it_can_be_written(
    consumer_settings: Settings,
) -> None:
    """The dead-letter topic doesn't exist yet and topics aren't created automatically, so the
    write isn't confirmed within 10 s. Nothing may be committed without the dead letter; once the
    topic exists, the next probe writes it and the offset is committed."""
    settings = dataclasses.replace(
        consumer_settings,
        kafka_dead_letter_topic=f"{consumer_settings.kafka_topic}.dlt-not-yet",
        pause_probe_interval_seconds=1.0,
    )
    publish_records(settings.kafka_topic, [(b"key", b"not json", [])])

    with running(settings):
        paused = eventually(
            lambda: heartbeat(settings) or {},
            until=lambda beat: beat.get("state") == "paused",
            what="the consumer to pause on the unwritable dead letter",
        )
        assert sum(lag(group_of(settings)).values()) == 1, "nothing committed"

        create_topic(settings.kafka_dead_letter_topic)
        consumers = group_of(settings)
        [dead_letter] = wait_for_dead_letters(consumers)
        assert_all_committed(consumers)

    assert paused["reason"] == "systemic: dead-letter-topic-unwritable"
    assert dead_letter.header("dlt.reason") == "invalid"
    beat = heartbeat(settings)
    assert beat is not None and beat["state"] == "running"


def test_an_instance_paused_longer_than_its_limit_exits_with_code_3(
    consumer_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as the container runs it: settings from the environment, through `main`."""
    settings = consumer_settings
    order_id = uuid4()
    publish(settings.kafka_topic, [(order_id, order_placed_event(order_id))])
    environment = {
        "KAFKA_BOOTSTRAP_SERVERS": KAFKA_BOOTSTRAP,
        "KAFKA_TOPIC": settings.kafka_topic,
        "KAFKA_DEAD_LETTER_TOPIC": settings.kafka_dead_letter_topic,
        "KAFKA_GROUP_ID": settings.kafka_group_id,
        "DATABASE_URL": CONSUMER_DATABASE_URL,
        "FULFILMENT_BASE_URL": STUB_URL,
        "FULFILMENT_TOKEN": "not-the-token",
        "ORDER_EVENT_SCHEMA": str(EVENT_SCHEMA),
        "HEALTH_FILE": str(tmp_path / "health.json"),
        "PAUSE_PROBE_INTERVAL_SECONDS": "1",
        "PAUSE_EXIT_AFTER_MINUTES": "0.05",  # 3 s
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    # A deadline: should it never give up, SIGTERM stops it cleanly, with exit code 0. `main`
    # installs its own signal handlers; the test process gets its own back afterwards.
    handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    deadline = threading.Timer(30.0, os.kill, (os.getpid(), signal.SIGTERM))
    deadline.start()
    try:
        exit_code = main()
    finally:
        deadline.cancel()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)

    assert exit_code == EXIT_PAUSED_TOO_LONG

    assert sum(lag(group_of(settings)).values()) == 1, "nothing committed"
    assert records(settings.kafka_dead_letter_topic) == []
