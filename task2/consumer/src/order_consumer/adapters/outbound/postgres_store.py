"""The order store on PostgreSQL, in plain SQL (ADR 0002)."""

import time
from collections.abc import Callable
from datetime import UTC
from typing import Literal
from uuid import UUID

import psycopg
from psycopg import IsolationLevel

from order_consumer.application.ports import (
    AlreadyStored,
    EventIdTaken,
    HandoverStatus,
    ReceivedEvent,
    Stored,
    StoreUnavailable,
)
from order_consumer.domain.order import Order, OrderLine

# Three quick reconnects within about 5 s before the database counts as unavailable (ADR 0006).
RECONNECT_DELAYS_SECONDS = (0.5, 1.0, 2.0)
CONNECT_TIMEOUT_SECONDS = 2


class PostgresOrderStore:
    """One connection per consumer process: processing is one message at a time (ADR 0005).

    A step that fails because the database can't be reached, or the connection broke, is done
    again on a new connection. Every step is safe to repeat, even after a commit whose answer was
    lost (ADR 0002, 0004).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connection: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(
                self._database_url, connect_timeout=CONNECT_TIMEOUT_SECONDS
            )
            # Explicit, since storing relies on it: see `_stored_version`.
            self._connection.isolation_level = IsolationLevel.READ_COMMITTED
        return self._connection

    def _reconnecting[T](self, step: Callable[[psycopg.Connection], T]) -> T:
        for delay in RECONNECT_DELAYS_SECONDS:
            try:
                return step(self._connect())
            except psycopg.OperationalError:
                self.close()
                time.sleep(delay)
        try:
            return step(self._connect())
        except psycopg.OperationalError as error:
            self.close()
            raise StoreUnavailable(f"{type(error).__name__}: {error}") from error

    def store_new(
        self, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
        return self._reconnecting(lambda connection: self._store_new(connection, order, event))

    def _store_new(
        self, connection: psycopg.Connection, order: Order, event: ReceivedEvent
    ) -> Stored | AlreadyStored | EventIdTaken:
        with connection.transaction() as transaction, connection.cursor() as cursor:
            # The primary key is the final guard: of two consumers racing on one order, the second
            # waits for the first to commit, then stores nothing (ADR 0002).
            cursor.execute(
                """
                INSERT INTO orders
                    (order_id, customer_id, placed_at, currency, total, item_count,
                     first_event_id, handover_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'awaiting_handover')
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
        def record(connection: psycopg.Connection) -> None:
            with connection.transaction(), connection.cursor() as cursor:
                self._record(cursor, order_id, event, "duplicate")

        self._reconnecting(record)

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
            INSERT INTO processed_events (event_id, order_id, outcome, topic, partition, "offset")
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (event.event_id, order_id, outcome, event.topic, event.partition, event.offset),
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
        self._reconnecting(
            lambda connection: self._record_handover(connection, order_id, fulfilment_order_id)
        )

    @staticmethod
    def _record_handover(
        connection: psycopg.Connection, order_id: UUID, fulfilment_order_id: str
    ) -> None:
        with connection.transaction():
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
        self._reconnecting(lambda connection: self._mark_handover_failed(connection, order_id))

    @staticmethod
    def _mark_handover_failed(connection: psycopg.Connection, order_id: UUID) -> None:
        with connection.transaction():
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
            self._connection = None
