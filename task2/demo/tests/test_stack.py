"""Reading a partition's watermarks survives a broker connection that is still settling."""

import pytest
from confluent_kafka import KafkaError, KafkaException, TopicPartition

from demo.stack import watermarks

PARTITION = TopicPartition("orders.dlt", 0)


class Reader:
    """Fails the first `failures` queries with a local connection error, then answers."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.queries = 0

    def get_watermark_offsets(self, partition: TopicPartition, timeout: float) -> tuple[int, int]:
        self.queries += 1
        if self.queries <= self.failures:
            raise KafkaException(KafkaError(KafkaError._TRANSPORT))
        return (0, 5)


def test_a_query_that_fails_while_the_connection_settles_is_asked_again() -> None:
    reader = Reader(failures=2)

    assert watermarks(reader, PARTITION, within=1.0, pause=0.0) == (0, 5)
    assert reader.queries == 3


def test_a_query_that_keeps_failing_raises_its_error_in_the_end() -> None:
    reader = Reader(failures=1_000_000)

    with pytest.raises(KafkaException):
        watermarks(reader, PARTITION, within=0.05, pause=0.01)
