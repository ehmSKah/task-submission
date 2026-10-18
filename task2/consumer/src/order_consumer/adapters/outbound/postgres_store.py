"""The order store on PostgreSQL, in plain SQL (ADR 0002)."""

from uuid import UUID

import psycopg

from order_consumer.domain.order import Order


class PostgresOrderStore:
    """One connection per consumer process: processing is one message at a time (ADR 0005)."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._connection: psycopg.Connection | None = None

    def _connect(self) -> psycopg.Connection:
        if self._connection is None or self._connection.closed:
            self._connection = psycopg.connect(self._database_url)
        return self._connection

    def store_new(self, order: Order, event_id: UUID) -> None:
        connection = self._connect()
        with connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO orders
                    (order_id, customer_id, placed_at, currency, total, item_count,
                     first_event_id, handover_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'awaiting_handover')
                """,
                (
                    order.order_id,
                    order.customer_id,
                    order.placed_at,
                    order.currency,
                    order.total,
                    order.item_count,
                    event_id,
                ),
            )
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

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        connection = self._connect()
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

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
