import io
import logging
from collections.abc import Iterator

import pytest

from orders_api.adapters.inbound import api_handler


@pytest.fixture
def log_output() -> Iterator[io.StringIO]:
    """Capture the API function's structured log output (Powertools binds stdout at import)."""
    stream = io.StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(api_handler.logger.registered_formatter)
    api_handler.logger.addHandler(capture)
    yield stream
    api_handler.logger.removeHandler(capture)
