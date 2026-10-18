"""The order store on PostgreSQL, in plain SQL (ADR 0002).

Each write is a span of its own, named and described by OpenTelemetry's database conventions, and
never carries the connection string or a statement's values (ADR 0008).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from typing import Literal
from uuid import UUID

import psycopg
from opentelemetry import trace
from psycopg import IsolationLevel

from order_consumer.application.ports import (
    AlreadyStored,
    EventIdTaken,
    HandoverStatus,
    ReceivedEvent,
    Stored,
)
from order_consumer.domain.order import Order, OrderLine

tracer = trace.get_tracer(__name__)


class PostgresOrderStore:
    """One connection per consumer process: processing is one message at a time (ADR 0005)."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connection: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(self._database_url)
            # Explicit, since storing relies on it: see `_stored_version`.
            self._connection.isolation_level = IsolationLevel.READ_COMMITTED
        return self._connection

    @contextmanager
    def _write(self, summary: str) -> Iterator[psycopg.Connection]:
        """The connection, inside a span for one write: `summary` names the operation and the
        tables it writes, e.g. `UPDATE orders`."""
        with tracer.start_as_current_span(summary, kind=trace.SpanKind.CLIENT) as span:
            connection = self._connect()
            info = connection.info
            span.set_attributes(
                {
                    "db.system.name": "postgresql",
                    "db.namespace": info.dbname,
                    "db.query.summary": summary,
                    "server.address": info.host,
                    "server.port": info.port,
                }
            )
            yield connection

    def store_new(
        self, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
        with (
            self._write("INSERT orders order_lines processed_events") as connection,
            connection.transaction() as transaction,
            connection.cursor() as cursor,
        ):
            # The primary key is the final guard: of two consumers racing on one order, the second
            # waits for the first to commit, then stores nothing (ADR 0002).
            cursor.execute(
                """
                INSERT INTO orders
                    (order_id, customer_id, placed_at, currency, total, item_count,
                     first_event_id, trace_id, handover_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'awaiting_handover')
                ON CONFLICT (order_id) DO NOTHING
                """,
                (
                    order.order_id,
                    order.customer_id,
                    order.placed_at,
                    order.currency,
                    order.total,
                    order.item_count,
                    event.event_id,
                    event.trace_id,
                ),
            )
            if cursor.rowcount == 0:
                return self._stored_version(cursor, order.order_id, event.event_id)
            if not self._record(cursor, order.order_id, event, "accepted"):
                # The event id already brought another order: a conflict, so store nothing.
                raise psycopg.Rollback(transaction)
            cursor.executemany(
                """
                INSERT INTO order_lines (order_id, sku, quantity, unit_price, line_total)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (order.order_id, line.sku, line.quantity, line.unit_price, line.line_total)
                    for line in order.lines
                ],
            )
            return Stored()
        return EventIdTaken()

    def record_duplicate(self, order_id: UUID, event: ReceivedEvent) -> None:
        with (
            self._write("INSERT processed_events") as connection,
            connection.transaction(),
            connection.cursor() as cursor,
        ):
            self._record(cursor, order_id, event, "duplicate")

    @staticmethod
    def _record(
        cursor: psycopg.Cursor,
        order_id: UUID,
        event: ReceivedEvent,
        outcome: Literal["accepted", "duplicate"],
    ) -> bool:
        """Records the event; False, writing nothing, when its id is already recorded."""
        cursor.execute(
            """
            INSERT INTO processed_events
                (event_id, order_id, outcome, topic, partition, "offset", trace_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (
                event.event_id,
                order_id,
                outcome,
                event.topic,
                event.partition,
                event.offset,
                event.trace_id,
            ),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _stored_version(
        cursor: psycopg.Cursor, order_id: UUID, event_id: UUID
    ) -> AlreadyStored | EventIdTaken:
        """The stored version, unless the event id is recorded for another order. Read in the
        inserting transaction: under read committed, each statement sees what a racing consumer
        committed before the insert gave way."""
        cursor.execute(
            """
            SELECT customer_id, placed_at, currency, total, first_event_id, handover_status,
                   (SELECT order_id FROM processed_events WHERE event_id = %s)
            FROM orders WHERE order_id = %s
            """,
            (event_id, order_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError(f"order {order_id} is not stored")
        customer_id, placed_at, currency, total, first_event_id, handover_status, recorded_for = row
        if recorded_for not in (None, order_id):
            return EventIdTaken()
        cursor.execute(
            "SELECT sku, quantity, unit_price FROM order_lines WHERE order_id = %s ORDER BY sku",
            (order_id,),
        )
        lines = tuple(
            OrderLine(sku=sku, quantity=quantity, unit_price=unit_price)
            for sku, quantity, unit_price in cursor.fetchall()
        )
        return AlreadyStored(
            order=Order(
                order_id=order_id,
                customer_id=customer_id,
                placed_at=placed_at.astimezone(UTC),
                currency=currency,
                lines=lines,
                total=total,
            ),
            first_event_id=first_event_id,
            handover_status=HandoverStatus(handover_status),
            event_recorded=recorded_for == order_id,
        )

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        with self._write("UPDATE orders") as connection, connection.transaction():
            connection.execute(
                """
                UPDATE orders
                SET handover_status = 'handed_over',
                    fulfilment_order_id = %s,
                    handed_over_at = now()
                WHERE order_id = %s
                """,
                (fulfilment_order_id, order_id),
            )

    def mark_handover_failed(self, order_id: UUID) -> None:
        with self._write("UPDATE orders") as connection, connection.transaction():
            # Never over a completed handover: of two consumers racing on one order, the one that
            # gave up must not undo the other's success.
            connection.execute(
                """
                UPDATE orders
                SET handover_status = 'handover_failed'
                WHERE order_id = %s AND handover_status <> 'handed_over'
                """,
                (order_id,),
            )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
