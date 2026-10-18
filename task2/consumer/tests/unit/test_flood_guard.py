"""The flood guard (ADR 0006): when failures stop being about single messages, the message in hand
is held and consumption stops, instead of the dead-letter topic filling with healthy orders.

Three consecutive orders running out of retries open the circuit; while it is open, each handover
is one attempt, a probe, and an answer from the fulfilment service closes it. Five consecutive
unexpected exceptions trip the bug guard. Invalid and conflicting events never count."""

import dataclasses
from uuid import UUID, uuid4

from structlog.testing import capture_logs

from consumer.tests.unit.test_processing import (
    CONFLICTING_LINES,
    ProcessorWithFakes,
    delivery,
    reader,
    unavailable,
)
from order_consumer.application.flood_guard import FloodGuardThresholds
from order_consumer.application.ports import (
    DeadLetterReason,
    Delivery,
    HandoverFailed,
    ResponseClass,
)
from order_consumer.application.processing import OrderProcessor, Outcome, Stopped, StopReason
from tests.support.events import order_placed_event

ATTEMPTS = 5  # per handover, by default


def order(order_id: UUID, value: bytes | None = None) -> Delivery:
    value = order_placed_event(order_id) if value is None else value
    return dataclasses.replace(delivery(value), key=str(order_id).encode())


def invalid() -> Delivery:
    order_id = uuid4()
    return order(order_id, order_placed_event(order_id, total=1))


def conflicting(harness: ProcessorWithFakes) -> Delivery:
    """A second version of an order handed over before, with other lines."""
    order_id = uuid4()
    assert harness.processor.process(order(order_id)) is Outcome.HANDED_OVER
    return order(
        order_id, order_placed_event(order_id, uuid4(), lines=CONFLICTING_LINES, total=5997)
    )


def open_circuit(harness: ProcessorWithFakes) -> UUID:
    """Three orders in a row run out of retries; returns the third, held."""
    for _ in range(2):
        run_out_of_retries(harness, uuid4())
    held = uuid4()
    assert isinstance(run_out_of_retries(harness, held), Stopped)
    return held


def run_out_of_retries(harness: ProcessorWithFakes, order_id: UUID) -> Outcome | Stopped:
    harness.failures.extend(unavailable() for _ in range(ATTEMPTS))
    return harness.processor.process(order(order_id))


def test_the_third_consecutive_order_out_of_retries_opens_the_circuit_and_is_held() -> None:
    harness = ProcessorWithFakes()
    first, second, third = uuid4(), uuid4(), uuid4()

    outcomes = [run_out_of_retries(harness, order_id) for order_id in (first, second, third)]

    assert outcomes == [
        Outcome.DEAD_LETTERED,
        Outcome.DEAD_LETTERED,
        Stopped(StopReason.CIRCUIT_OPEN, "503 Service Unavailable"),
    ]
    assert [letter.order_id for letter in harness.dead_letters.written] == [first, second]
    assert harness.store.orders[third].handover_status == "awaiting_handover", "not given up"


def test_an_order_handed_over_in_between_starts_the_count_again() -> None:
    harness = ProcessorWithFakes()

    before = [run_out_of_retries(harness, uuid4()) for _ in range(2)]
    handed_over = harness.processor.process(order(uuid4()))
    after = [run_out_of_retries(harness, uuid4()) for _ in range(2)]

    assert before == after == [Outcome.DEAD_LETTERED] * 2
    assert handed_over is Outcome.HANDED_OVER
    assert isinstance(run_out_of_retries(harness, uuid4()), Stopped), "the third in a row"


def test_while_the_circuit_is_open_each_probe_is_one_attempt_without_waiting() -> None:
    """The poll loop probes every 30 s (ADR 0006); a probe must not spend another minute on
    retries, nor dead-letter the order when it fails."""
    harness = ProcessorWithFakes()
    held = open_circuit(harness)
    attempts, waits = len(harness.fulfilment.requests), len(harness.clock.sleeps)
    harness.failures.extend(unavailable() for _ in range(ATTEMPTS))

    with capture_logs() as logs:
        probes = [harness.processor.process(order(held)) for _ in range(2)]

    assert probes == [Stopped(StopReason.CIRCUIT_OPEN, "503 Service Unavailable")] * 2
    assert len(harness.fulfilment.requests) == attempts + 2
    assert len(harness.clock.sleeps) == waits
    assert len(harness.dead_letters.written) == 2, "only the two before the circuit opened"
    failed = [entry for entry in logs if entry["event"] == "circuit.probe_failed"]
    assert [entry["error"] for entry in failed] == ["503 Service Unavailable"] * 2


def test_a_successful_probe_closes_the_circuit_and_the_next_order_gets_its_retries() -> None:
    harness = ProcessorWithFakes()
    held = open_circuit(harness)

    with capture_logs() as logs:
        probe = harness.processor.process(order(held))

    assert probe is Outcome.HANDED_OVER
    assert harness.store.orders[held].handover_status == "handed_over"
    assert "circuit.closed" in [entry["event"] for entry in logs]
    harness.failures.extend(unavailable() for _ in range(2))
    assert harness.processor.process(order(uuid4())) is Outcome.HANDED_OVER, "after retries"


def test_a_probe_the_service_rejects_closes_the_circuit_and_dead_letters_the_order() -> None:
    """The service decided on the order: it is up, and the order is bad for it (ADR 0003)."""
    harness = ProcessorWithFakes()
    held = open_circuit(harness)
    harness.failures.append(HandoverFailed(ResponseClass.REJECTED, "422", status=422))

    assert harness.processor.process(order(held)) is Outcome.DEAD_LETTERED

    assert harness.dead_letters.written[-1].reason is DeadLetterReason.REJECTED
    harness.failures.extend(unavailable() for _ in range(2))
    assert harness.processor.process(order(uuid4())) is Outcome.HANDED_OVER, "after retries"


def test_orders_ended_by_a_long_retry_after_count_as_out_of_retries() -> None:
    """The service said it is unavailable for a while: the flood guard's business (ADR 0006)."""
    harness = ProcessorWithFakes()

    outcomes = []
    for _ in range(3):
        harness.failures.append(unavailable(retry_after=120.0))
        outcomes.append(harness.processor.process(order(uuid4())))

    assert outcomes[:2] == [Outcome.DEAD_LETTERED] * 2
    assert outcomes[2] == Stopped(StopReason.CIRCUIT_OPEN, "503 Service Unavailable")


def test_a_probe_meeting_a_systemic_answer_pauses_on_with_that_reason_the_circuit_still_open() -> (
    None
):
    harness = ProcessorWithFakes()
    held = open_circuit(harness)
    harness.failures.append(HandoverFailed(ResponseClass.SYSTEMIC, "401 Unauthorized", status=401))

    assert harness.processor.process(order(held)) == Stopped(
        StopReason.FULFILMENT, "401 Unauthorized"
    )
    attempts = len(harness.fulfilment.requests)
    harness.failures.append(unavailable())
    assert harness.processor.process(order(held)) == Stopped(
        StopReason.CIRCUIT_OPEN, "503 Service Unavailable"
    )
    assert len(harness.fulfilment.requests) == attempts + 1, "still a probe"


def test_an_order_out_of_retries_is_counted_once_its_dead_letter_is_written() -> None:
    """Without the dead letter it is processed again after the pause, and counted only then."""
    harness = ProcessorWithFakes()
    first = uuid4()
    harness.dead_letters.fails = True
    assert run_out_of_retries(harness, first) == Stopped(
        StopReason.DEAD_LETTER_TOPIC_UNWRITABLE, "not confirmed in time"
    )
    harness.dead_letters.fails = False

    outcomes = [run_out_of_retries(harness, order_id) for order_id in (first, uuid4())]

    assert outcomes == [Outcome.DEAD_LETTERED] * 2, "the second order is only the second"
    assert isinstance(run_out_of_retries(harness, uuid4()), Stopped)


def hit_a_bug(harness: ProcessorWithFakes, order_id: UUID) -> Outcome | Stopped:
    harness.failures.append(RuntimeError("a bug"))
    return harness.processor.process(order(order_id))


def test_the_fifth_consecutive_unexpected_exception_trips_the_bug_guard_and_is_held() -> None:
    """A bug that fails every message waits in Kafka for its fix (ADR 0006)."""
    harness = ProcessorWithFakes()
    orders = [uuid4() for _ in range(5)]

    with capture_logs() as logs:
        outcomes = [hit_a_bug(harness, order_id) for order_id in orders]

    assert outcomes == [Outcome.DEAD_LETTERED] * 4 + [
        Stopped(StopReason.BUG_GUARD, "RuntimeError: a bug")
    ]
    assert [letter.order_id for letter in harness.dead_letters.written] == orders[:4]
    [tripped] = [entry for entry in logs if entry["event"] == "bug_guard.tripped"]
    assert tripped["error"] == "RuntimeError: a bug"


def test_a_message_processed_without_a_bug_releases_the_bug_guard_and_restarts_the_count() -> None:
    """A probe of the held message succeeds once the cause is gone, e.g. a missing grant."""
    harness = ProcessorWithFakes()
    held = uuid4()
    for _ in range(4):
        hit_a_bug(harness, uuid4())
    assert isinstance(hit_a_bug(harness, held), Stopped)
    assert isinstance(hit_a_bug(harness, held), Stopped), "a probe meeting the bug again"

    assert harness.processor.process(order(held)) is Outcome.HANDED_OVER

    outcomes = [hit_a_bug(harness, uuid4()) for _ in range(5)]
    assert outcomes[:4] == [Outcome.DEAD_LETTERED] * 4
    assert isinstance(outcomes[4], Stopped)


def test_a_stream_of_invalid_and_conflicting_events_never_trips_either_guard() -> None:
    """A producer sending bad data fills the dead-letter topic; stopping would hold up the valid
    events behind them (ADR 0006)."""
    harness = ProcessorWithFakes()
    bad = [invalid() if n % 2 else conflicting(harness) for n in range(20)]

    outcomes = [harness.processor.process(event) for event in bad]

    assert outcomes == [Outcome.DEAD_LETTERED] * 20
    reasons = {letter.reason for letter in harness.dead_letters.written}
    assert reasons == {DeadLetterReason.INVALID, DeadLetterReason.CONFLICT}


def test_invalid_and_conflicting_events_in_between_dont_end_a_run_of_orders_out_of_retries() -> (
    None
):
    """They say nothing about the fulfilment service."""
    harness = ProcessorWithFakes()
    bad = [invalid(), conflicting(harness)]

    run_out_of_retries(harness, uuid4())
    for event in bad:
        harness.processor.process(event)
        outcome = run_out_of_retries(harness, uuid4())

    assert outcome == Stopped(StopReason.CIRCUIT_OPEN, "503 Service Unavailable")


def test_invalid_and_conflicting_events_in_between_dont_end_a_run_of_unexpected_exceptions() -> (
    None
):
    """They say nothing about the code: bad data is caught before it could meet the bug."""
    harness = ProcessorWithFakes()
    bad = [invalid(), conflicting(harness), invalid(), conflicting(harness)]

    hit_a_bug(harness, uuid4())
    for event in bad:
        harness.processor.process(event)
        outcome = hit_a_bug(harness, uuid4())

    assert outcome == Stopped(StopReason.BUG_GUARD, "RuntimeError: a bug")


def test_the_thresholds_are_configuration() -> None:
    harness = ProcessorWithFakes()
    harness.processor = OrderProcessor(
        harness.store,
        harness.fulfilment,
        harness.dead_letters,
        reader,
        policy=harness.policy,
        clock=harness.clock,
        jitter=harness.jitter,
        stop=harness.stop,
        thresholds=FloodGuardThresholds(circuit_breaker=1, bug_guard=2),
    )

    assert isinstance(run_out_of_retries(harness, uuid4()), Stopped), "the first order"
    harness.processor.process(order(uuid4()))  # closes the circuit
    assert hit_a_bug(harness, uuid4()) is Outcome.DEAD_LETTERED
    assert hit_a_bug(harness, uuid4()) == Stopped(StopReason.BUG_GUARD, "RuntimeError: a bug")
