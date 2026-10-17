"""In-memory stand-ins for the ports, so unit tests need no emulator (ADR 0011)."""

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime

from aws_lambda_powertools.utilities.idempotency import BasePersistenceLayer
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyItemAlreadyExistsError,
    IdempotencyItemNotFoundError,
)
from aws_lambda_powertools.utilities.idempotency.persistence.datarecord import (
    STATUS_CONSTANTS,
    DataRecord,
)

from orders_api.domain.order import Order


class InMemoryOrderRepository:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders = {order.id: order for order in orders}

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def add(self, order: Order) -> None:
        assert order.id not in self._orders, f"order {order.id} already exists"
        self._orders[order.id] = order

    # Race simulation and write counting; immutable class defaults, set per instance once used.
    _interleaved: Callable[[], object] | None = None
    interleaved = False
    """Whether the `interleave`d request has run."""
    updates_attempted = 0

    def update_status(self, current: Order, changed: Order, *, check_version: bool) -> Order | None:
        """The DynamoDB adapter's conditional write: applied only while the stored order still has
        `current`'s status (and version, if `check_version`); `None` otherwise."""
        self.updates_attempted += 1
        if self._interleaved is not None:
            concurrent, self._interleaved = self._interleaved, None
            concurrent()
            self.interleaved = True
        stored = self._orders.get(current.id)
        if (
            stored is None
            or stored.status is not current.status
            or (check_version and stored.version != current.version)
        ):
            return None
        updated = replace(
            stored, status=changed.status, updated_at=changed.updated_at, version=stored.version + 1
        )
        self._orders[current.id] = updated
        return updated

    def delete(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def interleave(self, concurrent: Callable[[], object]) -> None:
        """Run `concurrent` once, just before the next conditional write is decided, as a request
        that wins the race against it would."""
        self._interleaved = concurrent


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class SequentialIds:
    def __init__(self, ids: Iterable[str]) -> None:
        self._ids = iter(ids)

    def new_id(self) -> str:
        return next(self._ids)


class InMemoryIdempotencyStore(BasePersistenceLayer):
    """Powertools' persistence contract in memory, with the DynamoDB layer's put condition:
    a key is free when absent, expired, or in progress past the Lambda's remaining time."""

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[str, DataRecord] = {}
        self.saved_in_progress: list[DataRecord] = []

    def _get_record(self, idempotency_key: str) -> DataRecord:
        try:
            return self.records[idempotency_key]
        except KeyError:
            raise IdempotencyItemNotFoundError from None

    def _put_record(self, data_record: DataRecord) -> None:
        existing = self.records.get(data_record.idempotency_key)
        if existing is not None and not self._is_free(existing):
            raise IdempotencyItemAlreadyExistsError
        self.saved_in_progress.append(data_record)
        self.records[data_record.idempotency_key] = data_record

    def _update_record(self, data_record: DataRecord) -> None:
        previous = self.records[data_record.idempotency_key]
        data_record.in_progress_expiry_timestamp = previous.in_progress_expiry_timestamp
        self.records[data_record.idempotency_key] = data_record

    def _delete_record(self, data_record: DataRecord) -> None:
        self.records.pop(data_record.idempotency_key, None)

    @staticmethod
    def _is_free(record: DataRecord) -> bool:
        in_progress_expired = (
            record.status == STATUS_CONSTANTS["INPROGRESS"]
            and record.in_progress_expiry_timestamp is not None
            and record.in_progress_expiry_timestamp < time.time() * 1000
        )
        return record.is_expired or in_progress_expired

    def hold_in_progress(self) -> None:
        """Turn every record back into a request that is still running, as a concurrent
        duplicate would find it."""
        for key, record in self.records.items():
            self.records[key] = DataRecord(
                idempotency_key=key,
                status=STATUS_CONSTANTS["INPROGRESS"],
                expiry_timestamp=record.expiry_timestamp,
                in_progress_expiry_timestamp=int(time.time() * 1000) + 60_000,
                payload_hash=record.payload_hash,
            )


WireApi = Callable[..., InMemoryOrderRepository]
"""The `wire_api` fixture: (orders=(), *, ids=()) -> the repository the API function now uses."""
