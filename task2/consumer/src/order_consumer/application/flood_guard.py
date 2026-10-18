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


class FloodGuard:
    def __init__(self, thresholds: FloodGuardThresholds) -> None:
        self._thresholds = thresholds
        self._exhausted = 0  # consecutive orders dead-lettered after running out of retries
        self._unexpected = 0  # consecutive messages dead-lettered after an unexpected exception
        self.circuit_open = False
        self._bug_guard_tripped = False

    def holds_exhausted(self, error: str) -> bool:
        """An order ran out of retries, or its probe failed: whether it is held, because the
        circuit opens with it or is open."""
        if self.circuit_open:
            log.warning("circuit.probe_failed", error=error)
            return True
        if self._exhausted + 1 < self._thresholds.circuit_breaker:
            return False
        self.circuit_open = True
        log.warning("circuit.opened", orders=self._thresholds.circuit_breaker, error=error)
        return True

    def exhausted_dead_lettered(self) -> None:
        self._exhausted += 1

    def answered(self) -> None:
        """The fulfilment service decided on an order: it is not down."""
        self._exhausted = 0
        if self.circuit_open:
            self.circuit_open = False
            log.info("circuit.closed")

    def holds_unexpected(self, error: str) -> bool:
        """A message met an unexpected exception: whether it is held, because the bug guard trips
        with it or has tripped."""
        if self._bug_guard_tripped:
            return True
        if self._unexpected + 1 < self._thresholds.bug_guard:
            return False
        self._bug_guard_tripped = True
        log.warning("bug_guard.tripped", messages=self._thresholds.bug_guard, error=error)
        return True

    def unexpected_dead_lettered(self) -> None:
        self._unexpected += 1

    def processed(self) -> None:
        """A valid event reached its outcome without an unexpected exception."""
        self._unexpected = 0
        self._bug_guard_tripped = False
