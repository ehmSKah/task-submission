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

from orders_api.adapters.inbound.rate_limiting import report_rate_limiter_event
from orders_api.application.fail_open_counters import FailOpenCounterStore
from orders_api.application.orders import Clock, OrderPage, PagePosition
from orders_api.application.rate_limiting import RateLimitCounterStore, RateLimiter
from orders_api.domain.order import Order
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS, RateLimits


class InMemoryOrderRepository:
    def __init__(self, orders: Iterable[Order] = ()) -> None:
        self._orders = {order.id: order for order in orders}

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def add(self, order: Order) -> None:
        assert order.id not in self._orders, f"order {order.id} already exists"
        self._orders[order.id] = order

    def list_newest_first(
        self, *, owner_id: str | None, limit: int, after: PagePosition | None
    ) -> OrderPage:
        """Ordered by `createdAt#id` like the DynamoDB indexes. As with a DynamoDB `Limit`, a full
        page always names a next position, even when no order follows."""

        def sort_key(created_at: str, order_id: str) -> str:
            return f"{created_at}#{order_id}"

        listed = sorted(
            (o for o in self._orders.values() if owner_id is None or o.owner_id == owner_id),
            key=lambda o: sort_key(o.created_at, o.id),
            reverse=True,
        )
        if after is not None:
            start = sort_key(after.created_at, after.order_id)
            listed = [o for o in listed if sort_key(o.created_at, o.id) < start]
        page = tuple(listed[:limit])
        if len(page) < limit:
            return OrderPage(orders=page, next_position=None)
        last = page[-1]
        return OrderPage(
            orders=page, next_position=PagePosition(created_at=last.created_at, order_id=last.id)
        )

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


class InMemoryRateLimitCounters:
    """The counter store's conditional `ADD`: a counter below its limit goes up by one and says
    what it is now; one at its limit stays as it is and says `None`."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expiry: dict[str, datetime] = {}

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        count = self.counts.get(key, 0)
        if count >= limit:
            return None
        self.counts[key] = count + 1
        self.expiry[key] = expires_at
        return count + 1


class FailingRateLimitCounters:
    """A counter store that fails with an error the port doesn't name, as a defect would."""

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        raise ConnectionError("counter store unreachable")


class UnreliableRateLimitCounters(InMemoryRateLimitCounters):
    """In-memory counters that fail as the port says the real store may: set `failure` to a
    `CounterStoreUnavailable` or `CounterStoreThrottled`, or back to `None` once it recovers."""

    def __init__(self, failure: Exception | None = None) -> None:
        super().__init__()
        self.failure = failure
        self.calls = 0

    def increment(self, key: str, *, limit: int, expires_at: datetime) -> int | None:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return super().increment(key, limit=limit, expires_at=expires_at)


class ManualTime:
    """A monotonic time source that moves only when told to, in seconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordedEvents:
    """Operational events as reported, in order: `(event, fields)`."""

    def __init__(self) -> None:
        self.reported: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, /, **fields: object) -> None:
        self.reported.append((event, fields))

    @property
    def names(self) -> list[str]:
        return [event for event, _ in self.reported]


class SwitchableRateLimitConfig:
    """The rate-limit configuration, as an operator would change it at runtime."""

    def __init__(self, limits: RateLimits = DEFAULT_RATE_LIMITS) -> None:
        self.limits = limits

    def current(self) -> RateLimits:
        return self.limits


class RateLimitsInUse:
    """What the API function counts with in unit tests: in-memory counters and limits that a test
    can switch, read through a real `RateLimiter` and, as in the function, a fail-open store
    that logs its events (on a time source that only moves when told to)."""

    def __init__(self, clock: Clock) -> None:
        self.counters = InMemoryRateLimitCounters()
        self.config = SwitchableRateLimitConfig()
        self.time = ManualTime()
        self._clock = clock
        self.use_counters(self.counters)

    def use_counters(self, store: RateLimitCounterStore) -> None:
        """Count in `store` instead, e.g. one that fails."""
        self._store = FailOpenCounterStore(
            store, events=report_rate_limiter_event, time_source=self.time
        )

    def limiter(self) -> RateLimiter:
        return RateLimiter(self._store, self.config, self._clock)


WireApi = Callable[..., InMemoryOrderRepository]
"""The `wire_api` fixture: (orders=(), *, ids=()) -> the repository the API function now uses."""
