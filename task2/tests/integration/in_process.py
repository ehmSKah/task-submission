"""Running the consumer inside the test process, and crashing it on purpose (ADR 0007, 0009).

A crash point wraps a real adapter: the wrapped step completes against the real service, then the
consumer dies before the next step, exactly where a kill can't reliably be timed.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from order_consumer.application.ports import FulfilmentClient, OrderStore
from order_consumer.config import Settings
from order_consumer.main import build
from tests.integration.stack import ConsumerGroup


class SimulatedCrash(BaseException):
    """A process dying: a BaseException, so no handler for ordinary errors gets to run."""


class CrashAfter:
    """Delegates to the adapter, and crashes the consumer as soon as `step` has returned."""

    def __init__(self, adapter: object, step: str) -> None:
        self._adapter = adapter
        self._step = step

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._adapter, name)
        if name != self._step:
            return attribute

        def step_then_crash(*args: Any, **kwargs: Any) -> Any:
            attribute(*args, **kwargs)
            raise SimulatedCrash(f"crashed right after {name}")

        return step_then_crash


def crash_after_storing(store: OrderStore) -> OrderStore:
    return cast(OrderStore, CrashAfter(store, "store_new"))


def crash_after_recording(store: OrderStore) -> OrderStore:
    return cast(OrderStore, CrashAfter(store, "record_handover"))


def crash_after_handover(fulfilment: FulfilmentClient) -> FulfilmentClient:
    return cast(FulfilmentClient, CrashAfter(fulfilment, "hand_over"))


def group_of(settings: Settings) -> ConsumerGroup:
    return ConsumerGroup(
        topic=settings.kafka_topic,
        group=settings.kafka_group_id,
        dead_letter_topic=settings.kafka_dead_letter_topic,
    )


def run_until_crash(
    settings: Settings,
    wrap_store: Callable[[OrderStore], OrderStore] = lambda store: store,
    wrap_fulfilment: Callable[[FulfilmentClient], FulfilmentClient] = lambda client: client,
    timeout: float = 30.0,
) -> None:
    """Runs a consumer instance until its crash point fires; fails if it never does."""
    stop = threading.Event()
    deadline = threading.Timer(timeout, stop.set)
    deadline.start()
    try:
        build(settings, stop, wrap_store, wrap_fulfilment).run()
    except SimulatedCrash:
        return
    finally:
        deadline.cancel()
    raise AssertionError(f"the crash point was not reached within {timeout:.0f} s")


@contextmanager
def running(settings: Settings) -> Iterator[None]:
    """A consumer instance running in a thread for the duration of the block."""
    stop = threading.Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            build(settings, stop).run()
        except BaseException as error:
            failures.append(error)
            raise

    thread = threading.Thread(target=run, name=f"consumer-{settings.kafka_group_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=15)
    assert not thread.is_alive(), "the in-process consumer did not stop"
    assert not failures, f"the in-process consumer failed: {failures[0]!r}"
