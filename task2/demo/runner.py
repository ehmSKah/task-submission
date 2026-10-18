"""Runs the demo's sections against the stack, then sums up what happened (ADR 0009).

Each section publishes its samples, waits for the states that prove them and prints one ✓ line per
check, ending with the test file that proves the same automatically. It never sleeps for a fixed
time: every wait is for a state, and a wait that times out says what it last observed. The first
expectation not met prints ✗, stops the run and fails it.
"""

import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from demo.outcomes import TerminalLine, outcome_counts, terminal_lines
from demo.samples import Message, Sample
from demo.stack import DeadLetter, Endpoints, HandoverRequest, StoredOrder

DEAD_LETTERS_IN_KAFKA_UI = "/ui/clusters/task2/all-topics/orders.dlt/messages"


class StackPort(Protocol):
    """What the demo needs of the running stack (`demo.stack.Stack`)."""

    @property
    def endpoints(self) -> Endpoints: ...
    def send(self, messages: Sequence[Message]) -> list[str]: ...
    def log_lines(self) -> list[str]: ...
    def dead_letters(self) -> list[DeadLetter]: ...
    def stored_order(self, order_id: UUID) -> StoredOrder | None: ...
    def fulfilment_orders(self, order_id: UUID) -> list[str]: ...
    def handover_requests(self, order_id: UUID) -> list[HandoverRequest]: ...
    def add_fault(self, order_id: UUID, status: int, times: int) -> None: ...
    def clear_faults(self, order_id: UUID) -> None: ...
    def replay(self, arguments: Sequence[str]) -> tuple[int, list[str]]: ...
    def tracing(self) -> bool: ...


class Unmet(Exception):
    """An expectation the stack didn't meet; the message says what was expected and seen."""


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    proven_by: str  # the integration test file proving the same, from task2/
    steps: Callable[["Demo"], None]


class Demo:
    """What a section works with: the stack, the checks, and the run's own traces."""

    def __init__(
        self, stack: StackPort, out: Callable[[str], None], timeout: float, interval: float
    ) -> None:
        self.stack = stack
        self._out = out
        self._timeout = timeout
        self._interval = interval
        self.trace_ids: set[str] = set()
        self.published = 0
        self.replays = 0
        self.followed: tuple[UUID, str] | None = None

    def publish(self, published: Sample) -> Sample:
        self.stack.send(published.messages)
        self.trace_ids.update(message.trace_id for message in published.messages)
        self.published += len(published.messages)
        return published

    def replayed(self) -> None:
        """A message was fed back into the order topic by the replay command."""
        self.replays += 1

    def follow(self, order_id: UUID, trace_id: str) -> None:
        """The order the summary points at in Jaeger and PostgreSQL."""
        self.followed = (order_id, trace_id)

    def ok(self, text: str) -> None:
        self._out(f"   ✓ {text}")

    def show(self, text: str) -> None:
        self._out(f"     {text}")

    def wait[T](self, probe: Callable[[], T], until: Callable[[T], bool], what: str) -> T:
        observed = probe()
        waited = 0.0
        while not until(observed):
            if waited >= self._timeout:
                raise Unmet(f"timed out waiting for {what}; last observed: {observed!r}")
            _pause(self._interval)
            waited += self._interval
            observed = probe()
        return observed

    def check(self, met: bool, expected: str, observed: object) -> None:
        if not met:
            raise Unmet(f"expected {expected}, observed {observed!r}")

    def outcomes(self, messages: Iterable[Message], expected: Sequence[str]) -> list[TerminalLine]:
        """Waits for the terminal lines of the messages' traces, which must be `expected`, in
        order (e.g. `dead_letter.written (invalid)`)."""
        trace_ids = {message.trace_id for message in messages}
        lines = self.wait(
            lambda: terminal_lines(self.stack.log_lines(), trace_ids),
            lambda found: len(found) >= len(expected),
            f"{len(expected)} terminal log line(s)",
        )
        logged = [line.label for line in lines]
        if logged != list(expected):
            raise Unmet(f"expected {', '.join(expected)}, the consumers logged {', '.join(logged)}")
        return lines


def _pause(seconds: float) -> None:
    """The one place the demo waits: between two looks at the stack."""
    time.sleep(seconds)


def run(
    sections: Sequence[Section],
    stack: StackPort,
    out: Callable[[str], None] = print,
    timeout: float = 60.0,
    interval: float = 0.5,
) -> bool:
    """Every section in order, then the summary; False at the first expectation not met."""
    demo = Demo(stack, out, timeout, interval)
    try:
        for section in sections:
            out(section.title)
            section.steps(demo)
            out(f"   proven by {section.proven_by}")
            out("")
        out("Summary")
        _summary(demo, out)
    except Unmet as unmet:
        out(f"   ✗ {unmet}")
        return False
    return True


def _summary(demo: Demo, out: Callable[[str], None]) -> None:
    messages = demo.published + demo.replays
    try:
        lines = demo.wait(
            lambda: terminal_lines(demo.stack.log_lines(), demo.trace_ids),
            lambda found: len(found) >= messages,
            f"{messages} terminal log lines",
        )
    except Unmet:
        lines = terminal_lines(demo.stack.log_lines(), demo.trace_ids)
    if len(lines) != messages:
        raise Unmet(
            f"{len(lines)} terminal log lines for {messages} messages: "
            "every message ends in exactly one"
        )
    out(
        f"   {demo.published} published + {demo.replays} replayed = {messages} messages, "
        f"{len(lines)} terminal log lines: one each"
    )
    counts = outcome_counts(lines)
    width = max(map(len, counts), default=0)
    for label, count in counts.items():
        out(f"     {label:<{width}}  {count:>2}")

    reasons = Counter(
        letter.headers.get("dlt.reason", "-")
        for letter in demo.stack.dead_letters()
        if letter.trace_id in demo.trace_ids
    )
    if reasons:
        listed = ", ".join(f"{reason} {count}" for reason, count in reasons.items())
        out(f"   dead letters of this run on orders.dlt: {listed}")

    endpoints = demo.stack.endpoints
    out("   where to look:")
    out(f"     kafka-ui   {endpoints.kafka_ui}{DEAD_LETTERS_IN_KAFKA_UI} (the dlt.* headers)")
    if demo.followed is not None:
        order_id, trace_id = demo.followed
        if demo.stack.tracing():
            out(f"     Jaeger     {endpoints.jaeger_ui}/trace/{trace_id}")
            out(f"                order {order_id}: each attempt, the stub's spans, the replay")
        else:
            out("     Jaeger     receives no traces: `make up-tracing`, then `make demo`, to see")
            out("                the trace of each attempt, the stub's spans and the replay")
        out(
            "     make psql  SELECT handover_status, fulfilment_order_id, trace_id FROM orders "
            f"WHERE order_id = '{order_id}';"
        )
    else:
        out("     Jaeger     with `make up-tracing`: each order's trace")
    out("     make ps    each consumer's health, and why it is paused if it is")
