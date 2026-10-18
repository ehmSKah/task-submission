"""`make publish`: sample events onto the order topic, each line saying where to find it."""

import io
import re
from collections.abc import Sequence

import pytest

from demo.publish import run
from demo.samples import CUSTOMER_ID, Message


class FakeTopic:
    """Takes the messages as Kafka would, giving each its position."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def __call__(self, messages: Sequence[Message]) -> list[str]:
        self.messages.extend(messages)
        start = len(self.messages) - len(messages)
        return [f"orders/0/{start + n}" for n in range(len(messages))]


def publish(*argv: str) -> tuple[FakeTopic, list[str]]:
    topic, out = FakeTopic(), io.StringIO()
    assert run(argv, topic, out) == 0
    return topic, out.getvalue().splitlines()


def test_without_arguments_one_valid_event_is_published() -> None:
    topic, lines = publish()

    [message] = topic.messages
    [line] = lines
    order_id = message.key.decode()
    assert re.fullmatch(
        rf"valid +order {order_id} +event {message.event_id} +trace {message.trace_id} +orders/0/0",
        line,
    )


def test_each_kind_is_published_count_times_one_line_per_record() -> None:
    topic, lines = publish("redelivered", "unreadable", "--count", "2")

    assert len(topic.messages) == 2 * 2 + 2 * 1
    assert [line.split()[0] for line in lines] == ["redelivered"] * 4 + ["unreadable"] * 2
    assert len({message.key for message in topic.messages}) == 4, "a new order per sample"


def test_a_record_without_a_readable_event_id_shows_a_dash() -> None:
    _, [line] = publish("not-a-cloudevent")

    assert " event - " in line


def test_valid_events_can_be_in_another_currency() -> None:
    topic, _ = publish("valid", "--currency", "JPY")

    assert b'"currency": "JPY"' in topic.messages[0].value


def test_neither_a_body_nor_the_customer_id_is_printed() -> None:
    _, lines = publish(*(kind for kind in ("valid", "conflicting", "schema-violation")))

    output = "\n".join(lines)
    assert CUSTOMER_ID not in output
    assert "{" not in output


def test_an_unknown_kind_is_refused_and_nothing_is_published() -> None:
    topic = FakeTopic()

    with pytest.raises(SystemExit) as exited:
        run(["cancelled"], topic, io.StringIO())

    assert exited.value.code == 2
    assert topic.messages == []
