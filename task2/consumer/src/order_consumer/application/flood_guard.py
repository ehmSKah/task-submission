"""The flood guard (ADR 0006): it recognises when failures stop being about single messages, so
the message in hand is held and consumption stops, instead of healthy orders being dead-lettered.

The circuit breaker counts consecutive orders that ran out of retries: an outage of the fulfilment
service. The order that reaches the threshold is held rather than dead-lettered, and while the
circuit is open each handover is a single attempt, a probe: a failed probe holds its order too,
and the first answer from the service closes the circuit.

The bug guard counts consecutive unexpected exceptions: a bug that fails every message. The
message that reaches the threshold is held too, and so is every one after it that fails the same
way, until a message is processed without one.
"""

from dataclasses import dataclass

import structlog

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class FloodGuardThresholds:
    circuit_breaker: int = 3  # consecutive orders out of retries that open the circuit
    bug_guard: int = 5  # consecutive unexpected exceptions that trip the bug guard


@dataclass
class _Run:
    """Consecutive failures of one kind, and whether they reached the threshold."""

    threshold: int
    count: int = 0  # failures set aside as dead letters
    tripped: bool = False

    def holds(self) -> bool:
        """A failure that trips the guard, or comes once it has tripped, is held."""
        self.tripped = self.tripped or self.count + 1 >= self.threshold
        return self.tripped

    def end(self) -> bool:
        """Starts the count again; returns whether the guard had tripped."""
        tripped, self.count, self.tripped = self.tripped, 0, False
        return tripped


class FloodGuard:
    def __init__(self, thresholds: FloodGuardThresholds) -> None:
        self._exhausted = _Run(thresholds.circuit_breaker)
        self._unexpected = _Run(thresholds.bug_guard)

    def attempt_limit(self) -> int | None:
        """While the circuit is open, a handover is a single attempt, a probe; otherwise the retry
        policy decides."""
        return 1 if self._exhausted.tripped else None

    def holds_exhausted(self, error: str) -> bool:
        """An order ran out of retries, or its probe failed: whether it is held, because the
        circuit opens with it or is open."""
        was_open = self._exhausted.tripped
        if not self._exhausted.holds():
            return False
        if was_open:
            log.warning("circuit.probe_failed", error=error)
        else:
            log.warning("circuit.opened", threshold=self._exhausted.threshold, error=error)
        return True

    def exhausted_dead_lettered(self) -> None:
        self._exhausted.count += 1

    def answered(self) -> None:
        """The fulfilment service decided on an order: it is not down."""
        if self._exhausted.end():
            log.info("circuit.closed")

    def holds_unexpected(self, error: str) -> bool:
        """A message met an unexpected exception: whether it is held, because the bug guard trips
        with it or has tripped."""
        was_tripped = self._unexpected.tripped
        held = self._unexpected.holds()
        if held and not was_tripped:
            log.warning("bug_guard.tripped", threshold=self._unexpected.threshold, error=error)
        return held

    def unexpected_dead_lettered(self) -> None:
        self._unexpected.count += 1

    def processed(self) -> None:
        """A valid event reached its outcome without an unexpected exception."""
        self._unexpected.end()
