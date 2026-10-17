"""Waiting out eventual consistency in tests that read lists (ADRs 0005 and 0011)."""

import time
from collections.abc import Callable

LIST_TIMEOUT_SECONDS = 5.0


def eventually[T](
    check: Callable[[], T], timeout: float = LIST_TIMEOUT_SECONDS, interval: float = 0.25
) -> T:
    """`check()`'s result once it stops failing an assertion; its last failure after `timeout`.

    Lists read global secondary indexes, so an order written a moment ago may be missing from
    them for a short while. Reads by id are strongly consistent and never need this.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)
