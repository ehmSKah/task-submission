import io
import logging
from collections.abc import Iterator

import pytest

from orders_api.adapters.inbound import api_handler
from tests.unit.fakes import InMemoryIdempotencyStore


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
    monkeypatch.setattr(api_handler, "_idempotency_store", store)
    return store
