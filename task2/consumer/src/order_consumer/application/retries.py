"""One handover with bounded retries (ADR 0003, 0006).

The message stays in hand while it is retried: transient failures are retried with exponential
backoff and full jitter, a handover the fulfilment service refuses is given up at once, and a
systemic answer ends the attempts at once, for the caller to stop consumption.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from opentelemetry import trace

from order_consumer.application.ports import (
    Clock,
    DeadLetterReason,
    FulfilmentClient,
    HandoverFailed,
    HandoverResult,
    ResponseClass,
)
from order_consumer.domain.downstream import DownstreamRequest

log = structlog.get_logger()
tracer = trace.get_tracer(__name__)

# Given the longest wait allowed, returns how long to wait: anything from 0 up to it.
type Jitter = Callable[[float], float]


def full_jitter(source: random.Random) -> Jitter:
    """Spreads the retries of several consumer instances, so they don't hit a recovering service
    at the same moment."""
    return lambda longest: source.uniform(0.0, longest)


class RetryBudgetExceeded(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5  # the first and 4 retries
    backoff_base: float = 1.0
    backoff_cap: float = 15.0
    retry_after_cap: float = 15.0  # a longer Retry-After ends the retries

    def backoff(self, retry: int, jitter: Jitter) -> float:
        """The wait before the `retry`-th retry, counted from 1: full jitter between 0 and
        min(cap, base * 2^n), with n counted from 0."""
        return jitter(min(self.backoff_cap, self.backoff_base * 2 ** (retry - 1)))

    def worst_case_seconds(self, attempt_deadline: float) -> float:
        """Every attempt runs into its deadline, and each retry waits as long as it may: its
        backoff cap, or a Retry-After up to its cap, whichever is longer."""
        waits = sum(
            max(self.backoff(retry, lambda longest: longest), self.retry_after_cap)
            for retry in range(1, self.max_attempts)
        )
        return self.max_attempts * attempt_deadline + waits

    def check_budget(self, attempt_deadline: float, max_poll_interval_ms: int) -> None:
        """The message stays in hand while it is retried, without polling. A consumer that doesn't
        poll within max.poll.interval.ms loses its partitions, so a handover's retries may use
        at most half of it (ADR 0006)."""
        worst_case = self.worst_case_seconds(attempt_deadline)
        limit = max_poll_interval_ms / 1000 / 2
        if worst_case > limit:
            raise RetryBudgetExceeded(
                f"the retries of one handover can take {worst_case:g} s, more than half of "
                f"max.poll.interval.ms ({limit:g} s)"
            )


@dataclass(frozen=True, slots=True)
class HandedOver:
    result: HandoverResult
    attempts: int


@dataclass(frozen=True, slots=True)
class GaveUp:
    """A failed handover: the retries ran out, or the fulfilment service refused the order."""

    reason: DeadLetterReason  # retries-exhausted or rejected
    attempts: int
    last_failure: HandoverFailed
    retry_after: float | None = None  # a Retry-After too long to wait for, which ended the retries


@dataclass(frozen=True, slots=True)
class SystemicAnswer:
    """The fulfilment service answered in a way that is wrong for every order (ADR 0003)."""

    failure: HandoverFailed
    attempts: int


class HandoverRetries:
    def __init__(
        self, fulfilment: FulfilmentClient, policy: RetryPolicy, clock: Clock, jitter: Jitter
    ) -> None:
        self._fulfilment = fulfilment
        self._policy = policy
        self._clock = clock
        self._jitter = jitter

    def hand_over(
        self, request: DownstreamRequest, max_attempts: int | None = None
    ) -> HandedOver | GaveUp | SystemicAnswer:
        """Ends at the first systemic answer, without further attempts. `max_attempts` overrides
        the policy's, e.g. a single attempt to probe a service thought to be down."""
        policy = self._policy
        if max_attempts is None:
            max_attempts = policy.max_attempts
        attempt = 0
        while True:
            attempt += 1
            outcome = self._attempt(request, attempt, max_attempts)
            if isinstance(outcome, HandedOver):
                return outcome
            failure = outcome
            match failure.response_class:
                case ResponseClass.SYSTEMIC:
                    return SystemicAnswer(failure, attempt)
                case ResponseClass.REJECTED:
                    return GaveUp(DeadLetterReason.REJECTED, attempt, failure)
            retry_after = failure.retry_after
            if retry_after is not None and retry_after > policy.retry_after_cap:
                # The service is unavailable for a while: that is the flood guard's business,
                # not a reason to retry against it sooner (ADR 0006).
                return GaveUp(DeadLetterReason.RETRIES_EXHAUSTED, attempt, failure, retry_after)
            if attempt == max_attempts:
                return GaveUp(DeadLetterReason.RETRIES_EXHAUSTED, attempt, failure)
            if retry_after is None:
                retry_after = policy.backoff(attempt, self._jitter)
            self._clock.sleep(retry_after)

    def _attempt(
        self, request: DownstreamRequest, attempt: int, max_attempts: int
    ) -> HandedOver | HandoverFailed:
        """One attempt in a span of its own, carrying what its log line says (ADR 0008); the
        HTTP request's span, and the fulfilment service's, sit inside it."""
        with tracer.start_as_current_span(
            "handover attempt",
            attributes={"handover.attempt": attempt, "handover.max_attempts": max_attempts},
        ) as span:
            started = self._clock.monotonic()
            try:
                result = self._fulfilment.hand_over(request)
            except HandoverFailed as failure:
                fields = {
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "response_class": failure.response_class.value,
                    "status": failure.status,
                    "error": failure.error,
                }
                span.set_attributes(
                    {
                        f"handover.{name}": value
                        for name, value in fields.items()
                        if value is not None
                    }
                )
                span.set_status(trace.StatusCode.ERROR, failure.error)
                log.warning(
                    "handover.attempt_failed",
                    **fields,
                    duration_ms=round((self._clock.monotonic() - started) * 1000),
                )
                return failure
            span.set_attribute("handover.response_class", ResponseClass.SUCCESS.value)
            return HandedOver(result, attempt)
