"""The running Compose stack, as the demo sees it from the host (ADR 0009).

It publishes to Kafka and reads the dead-letter topic, PostgreSQL and the stub's journal through
the published ports, and the consumers' logs and the replay command through Docker Compose. The
host and ports come from the environment the Makefile sets, so one stack per Compose project works
on a shared Docker daemon too.
"""

import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
import psycopg
from confluent_kafka import (
    OFFSET_BEGINNING,
    Consumer,
    KafkaException,
    Message,
    Producer,
    TopicPartition,
)
from psycopg.rows import dict_row

from demo import samples

TASK2 = Path(__file__).parents[1]
TOPIC = "orders"
DEAD_LETTER_TOPIC = "orders.dlt"


class WatermarkReader(Protocol):
    def get_watermark_offsets(
        self, partition: TopicPartition, timeout: float
    ) -> tuple[int, int]: ...


def watermarks(
    reader: WatermarkReader, partition: TopicPartition, within: float = 10.0, pause: float = 0.2
) -> tuple[int, int]:
    """The partition's low and high offsets. The query is a single request that librdkafka doesn't
    retry, so it fails while the client's first broker connections are still settling, e.g. while
    `localhost` resolves to an unreachable `::1` first. It is asked again until `within` runs
    out."""
    deadline = time.monotonic() + within
    while True:
        try:
            return reader.get_watermark_offsets(partition, timeout=within)
        except KafkaException:
            if time.monotonic() > deadline:
                raise
            time.sleep(pause)


def _port(name: str, default: int) -> str:
    return os.environ.get(name, str(default))


@dataclass(frozen=True, slots=True)
class Endpoints:
    host: str
    kafka: str
    database_url: str  # the consumer's role: data rights only
    stub: str
    kafka_ui: str
    jaeger_ui: str

    @classmethod
    def from_env(cls) -> "Endpoints":
        host = os.environ.get("STACK_HOST", "127.0.0.1")
        return cls(
            host=host,
            kafka=f"{host}:{_port('KAFKA_PORT', 9094)}",
            database_url=(
                "postgresql://order_consumer:local-dev-consumer"
                f"@{host}:{_port('POSTGRES_PORT', 5432)}/orders"
            ),
            stub=f"http://{host}:{_port('STUB_PORT', 8081)}",
            kafka_ui=f"http://{host}:{_port('UI_PORT', 8080)}",
            jaeger_ui=f"http://{host}:{_port('JAEGER_UI_PORT', 16686)}",
        )


@dataclass(frozen=True, slots=True)
class StoredOrder:
    handover_status: str
    currency: str
    total: int
    fulfilment_order_id: str | None
    trace_id: str | None


@dataclass(frozen=True, slots=True)
class HandoverRequest:
    """One request in the stub's journal (ADR 0003)."""

    received_at: datetime
    status: int | None  # None while in progress, or if the connection was dropped
    total: dict[str, str] | None  # the amount and currency it asked for


@dataclass(frozen=True, slots=True)
class DeadLetter:
    position: str  # e.g. orders.dlt/1/42
    headers: dict[str, str]  # the last value of each header

    @property
    def trace_id(self) -> str | None:
        """From the `traceparent` copied from the original record (ADR 0006)."""
        parts = self.headers.get("traceparent", "").split("-")
        return parts[1] if len(parts) == 4 else None


class Stack:
    def __init__(self, endpoints: Endpoints) -> None:
        self.endpoints = endpoints

    def send(self, messages: Sequence[samples.Message]) -> list[str]:
        """Publishes in this order, keyed by order id; returns each record's position."""
        positions: dict[int, str] = {}
        errors: list[str] = []

        def delivered(index: int) -> Any:
            def report(error: Any, message: Message) -> None:
                if error is None:
                    positions[index] = f"{message.topic()}/{message.partition()}/{message.offset()}"
                else:
                    errors.append(str(error))

            return report

        producer = Producer({"bootstrap.servers": self.endpoints.kafka, "enable.idempotence": True})
        for index, message in enumerate(messages):
            producer.produce(
                TOPIC,
                key=message.key,
                value=message.value,
                headers=list(message.headers),
                on_delivery=delivered(index),
            )
        undelivered = producer.flush(30)
        if undelivered or errors:
            raise RuntimeError(
                f"{undelivered + len(errors)} of {len(messages)} records not delivered to Kafka "
                f"at {self.endpoints.kafka}: {', '.join(errors) or 'no answer in 30 s'}"
            )
        return [positions[index] for index in range(len(messages))]

    def log_lines(self) -> list[str]:
        """Every line the consumer containers have logged."""
        return self._compose("logs", "--no-color", "--no-log-prefix", "consumer").splitlines()

    def dead_letters(self) -> list[DeadLetter]:
        """Every record on the dead-letter topic, read with a throwaway group that never
        commits."""
        reader = Consumer(
            {
                "bootstrap.servers": self.endpoints.kafka,
                "group.id": f"demo-reader-{uuid4().hex[:12]}",
                "enable.auto.commit": False,
            }
        )
        try:
            topic = reader.list_topics(DEAD_LETTER_TOPIC, timeout=10).topics[DEAD_LETTER_TOPIC]
            partitions = [
                TopicPartition(DEAD_LETTER_TOPIC, p, OFFSET_BEGINNING) for p in topic.partitions
            ]
            expected = 0
            for partition in partitions:
                low, high = watermarks(reader, partition)
                expected += high - low
            reader.assign(partitions)
            found: list[DeadLetter] = []
            deadline = time.monotonic() + 10
            while len(found) < expected:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"read {len(found)} of the {expected} records on {DEAD_LETTER_TOPIC}"
                    )
                message = reader.poll(0.5)
                if message is None or message.error() is not None:
                    continue
                # The client's types also allow str values, though a consumed record's are bytes.
                headers = message.headers() or []
                pairs = headers.items() if isinstance(headers, dict) else headers
                found.append(
                    DeadLetter(
                        f"{message.topic()}/{message.partition()}/{message.offset()}",
                        {
                            name: value
                            if isinstance(value, str)
                            else (value or b"").decode(errors="replace")
                            for name, value in pairs
                        },
                    )
                )
            return found
        finally:
            reader.close()

    def stored_order(self, order_id: UUID) -> StoredOrder | None:
        with psycopg.connect(self.endpoints.database_url, row_factory=dict_row) as connection:
            row = connection.execute(
                "SELECT handover_status, currency, total, fulfilment_order_id, trace_id "
                "FROM orders WHERE order_id = %s",
                (order_id,),
            ).fetchone()
        return None if row is None else StoredOrder(**row)

    def fulfilment_orders(self, order_id: UUID) -> list[str]:
        """The ids of the fulfilment orders created for the order."""
        response = httpx.get(
            f"{self.endpoints.stub}/_control/fulfilment-orders",
            params={"externalReference": str(order_id)},
        )
        response.raise_for_status()
        return [created["id"] for created in response.json()]

    def handover_requests(self, order_id: UUID) -> list[HandoverRequest]:
        response = httpx.get(
            f"{self.endpoints.stub}/_control/requests",
            params={"externalReference": str(order_id)},
        )
        response.raise_for_status()
        return [
            HandoverRequest(
                datetime.fromisoformat(entry["receivedAt"]), entry["status"], entry.get("total")
            )
            for entry in response.json()
        ]

    def add_fault(self, order_id: UUID, status: int, times: int) -> None:
        """The stub answers the order's next `times` requests with `status` (ADR 0003)."""
        rule = {"externalReference": str(order_id), "times": times, "status": status}
        httpx.post(f"{self.endpoints.stub}/_control/fault-rules", json=rule).raise_for_status()

    def clear_faults(self, order_id: UUID) -> None:
        httpx.delete(
            f"{self.endpoints.stub}/_control/fault-rules",
            params={"externalReference": str(order_id)},
        ).raise_for_status()

    def replay(self, arguments: Sequence[str]) -> tuple[int, list[str]]:
        """`make replay`: its exit code and output lines."""
        done = subprocess.run(
            ["make", "--silent", "--no-print-directory", "replay", f"ARGS={' '.join(arguments)}"],
            cwd=TASK2,
            capture_output=True,
            text=True,
            check=False,
        )
        # Compose reports its progress on stderr: shown only when the command failed.
        output = done.stdout if done.returncode == 0 else done.stdout + done.stderr
        return done.returncode, output.splitlines()

    def tracing(self) -> bool:
        """Whether the consumers export their spans to a running Jaeger, as after
        `make up-tracing`; a later `make up` leaves Jaeger running but stops the export."""
        jaeger = self._compose("--profile", "tracing", "ps", "--status", "running", "-q", "jaeger")
        consumers = self._compose("ps", "--status", "running", "-q", "consumer").split()
        if not jaeger.strip() or not consumers:
            return False
        environment = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                *consumers,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        exporting = [
            line for line in environment if line.startswith("OTEL_EXPORTER_OTLP_ENDPOINT=")
        ]
        return len(exporting) == len(consumers) and all(
            line.partition("=")[2] for line in exporting
        )

    def _compose(self, *arguments: str) -> str:
        return subprocess.run(
            ["docker", "compose", *arguments], cwd=TASK2, capture_output=True, text=True, check=True
        ).stdout
