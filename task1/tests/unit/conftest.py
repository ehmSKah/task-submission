import io
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.domain.order import Order
from tests.unit.fakes import (
    FixedClock,
    InMemoryIdempotencyStore,
    InMemoryOrderRepository,
    RateLimitsInUse,
    SequentialIds,
    WireApi,
)

NOW = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)


@pytest.fixture
def wire_api(monkeypatch: pytest.MonkeyPatch) -> WireApi:
    """Point the API function at an in-memory repository, a fixed clock and known ids."""

    def wire(orders: Iterable[Order] = (), *, ids: Iterable[str] = ()) -> InMemoryOrderRepository:
        repository = InMemoryOrderRepository(orders)
        monkeypatch.setattr(api_handler, "orders", lambda: repository)
        monkeypatch.setattr(api_handler, "clock", FixedClock(NOW))
        monkeypatch.setattr(api_handler, "order_ids", SequentialIds(list(ids)))
        return repository

    return wire


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    """Capture the API function's structured log output (Powertools binds stdout at import)."""
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(api_handler.logger.registered_formatter)
    api_handler.logger.addHandler(capture)
    yield stream
    api_handler.logger.removeHandler(capture)


@pytest.fixture(autouse=True)
def idempotency_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryIdempotencyStore:
    """Every unit test gets a fresh in-memory idempotency store instead of the table."""
    store = InMemoryIdempotencyStore()
    monkeypatch.setattr(api_handler, "idempotency_store", lambda: store)
    return store


@pytest.fixture(autouse=True)
def rate_limits(monkeypatch: pytest.MonkeyPatch) -> RateLimitsInUse:
    """Every unit test counts requests in memory, at the default limits, on the fixed clock."""
    in_use = RateLimitsInUse(FixedClock(NOW))
    monkeypatch.setattr(api_handler, "rate_limiter", in_use.limiter)
    return in_use
