"""The demo's scenario: the assignment's four test categories against the running stack.

Every section publishes new orders, so the demo can run again and again on the same stack, and
looks only at its own orders and traces. The stack runs with its own settings: the order that runs
out of retries takes its real backoff, about 8 s.
"""

import json
from uuid import UUID

from demo.runner import Demo, Section, Unmet
from demo.samples import INVALID_KINDS, TOTAL, Kind, sample
from demo.stack import DeadLetter, StoredOrder

# Each currency's exponent decides the decimal amount handed over (ADR 0001).
AMOUNTS = (("EUR", "44.98"), ("JPY", "4498"), ("KWD", "4.498"))
FAULT_STATUS = 503
MAX_ATTEMPTS = 5  # the stack's HANDOVER_MAX_ATTEMPTS (ADR 0006)


def short(order_id: UUID) -> str:
    return f"{str(order_id)[:8]}…"


def stored_as(demo: Demo, order_id: UUID, status: str) -> StoredOrder:
    stored = demo.wait(
        lambda: demo.stack.stored_order(order_id),
        lambda found: found is not None and found.handover_status == status,
        f"order {short(order_id)} to be stored as {status}",
    )
    if stored is None:  # the wait made sure of it
        raise Unmet(f"order {short(order_id)} is not stored")
    return stored


def one_fulfilment_order(demo: Demo, order_id: UUID) -> str:
    created = demo.stack.fulfilment_orders(order_id)
    demo.check(len(created) == 1, f"one fulfilment order for {short(order_id)}", created)
    return created[0]


def dead_letter_of(demo: Demo, trace_id: str) -> tuple[DeadLetter, dict[str, object]]:
    """The dead letter of the record that started this trace, and its `dlt.detail`."""
    [letter] = demo.wait(
        lambda: [letter for letter in demo.stack.dead_letters() if letter.trace_id == trace_id],
        lambda found: len(found) == 1,
        f"one dead letter in trace {trace_id}",
    )
    detail: dict[str, object] = json.loads(letter.headers.get("dlt.detail", "{}"))
    return letter, detail


def nothing_accepted(demo: Demo, order_id: UUID) -> None:
    stored, created = demo.stack.stored_order(order_id), demo.stack.fulfilment_orders(order_id)
    demo.check(stored is None and not created, "nothing stored, nothing handed over", created)


def successful_processing(demo: Demo) -> None:
    for currency, amount in AMOUNTS:
        order = demo.publish(sample(Kind.VALID, currency=currency))
        demo.outcomes(order.messages, ["handover.completed"])
        stored = stored_as(demo, order.order_id, "handed_over")
        created = one_fulfilment_order(demo, order.order_id)
        demo.check(created == stored.fulfilment_order_id, "the fulfilment order stored", stored)
        received = [
            request.total
            for request in demo.stack.handover_requests(order.order_id)
            if request.status == 201
        ]
        expected = {"amount": amount, "currency": currency}
        demo.check(received == [expected], f"{amount} {currency} handed over", received)
        demo.ok(
            f"order {short(order.order_id)}  {stored.total} {currency} stored, "
            f"{amount} {currency} handed over as {created}"
        )
    demo.show("stored in integer minor units, handed over in the currency's decimals;")
    demo.show("one fulfilment order and one terminal log line per order")


def invalid_messages(demo: Demo) -> None:
    for kind in INVALID_KINDS:
        invalid = demo.publish(sample(kind))
        [message] = invalid.messages
        demo.outcomes(invalid.messages, ["dead_letter.written (invalid)"])
        _, detail = dead_letter_of(demo, message.trace_id)
        demo.check(detail.get("layer") == kind.value, f"layer {kind.value}", detail.get("layer"))
        nothing_accepted(demo, invalid.order_id)
        errors = detail.get("errors")
        count = detail.get("errorCount")
        if not isinstance(errors, list) or not errors or not isinstance(count, int):
            raise Unmet(f"expected the errors in dlt.detail, observed {detail!r}")
        first, more = errors[0], count - 1
        where = first.get("pointer") or "the record"
        also = f" (and {more} more)" if more else ""
        demo.ok(f"{kind.value:<27}  dead letter: {where}: {first.get('message')}{also}")
    demo.show("each a dead letter naming its layer and errors, keeping the original record;")
    demo.show("nothing stored, nothing handed over, the offset committed")


def duplicate_events(demo: Demo) -> None:
    redelivered = demo.publish(sample(Kind.REDELIVERED))
    demo.outcomes(redelivered.messages, ["handover.completed", "order.duplicate (redelivery)"])
    created = one_fulfilment_order(demo, redelivered.order_id)
    demo.ok(f"redelivery (the same record twice): order.duplicate; still only {created}")

    re_emitted = demo.publish(sample(Kind.RE_EMITTED))
    demo.outcomes(re_emitted.messages, ["handover.completed", "order.duplicate (re-emission)"])
    created = one_fulfilment_order(demo, re_emitted.order_id)
    demo.ok(f"re-emission (a new event id, the same order): order.duplicate; still only {created}")

    conflicting = demo.publish(sample(Kind.CONFLICTING))
    other = conflicting.messages[1]
    demo.outcomes(conflicting.messages, ["handover.completed", "dead_letter.written (conflict)"])
    _, detail = dead_letter_of(demo, other.trace_id)
    fields = detail.get("fields")
    demo.check(fields == ["/data/lines", "/data/total"], "the differing fields", fields)
    stored = stored_as(demo, conflicting.order_id, "handed_over")
    demo.check(stored.total == TOTAL, f"the first version's total, {TOTAL}", stored.total)
    created = one_fulfilment_order(demo, conflicting.order_id)
    demo.ok(
        "conflicting event (other lines and total): dead letter naming /data/lines and "
        f"/data/total; the first version stays, {created}"
    )
    demo.show("duplicates are skipped; the first version of an order wins")


def downstream_failures(demo: Demo) -> None:
    flaky = sample(Kind.VALID)
    demo.stack.add_fault(flaky.order_id, FAULT_STATUS, times=2)
    demo.publish(flaky)
    demo.outcomes(flaky.messages, ["handover.completed"])
    requests = demo.stack.handover_requests(flaky.order_id)
    statuses = [request.status for request in requests]
    demo.check(statuses == [FAULT_STATUS, FAULT_STATUS, 201], "503, 503, 201", statuses)
    start = requests[0].received_at
    timeline = ", ".join(
        f"{request.status} at {(request.received_at - start).total_seconds():.1f} s"
        for request in requests
    )
    created = one_fulfilment_order(demo, flaky.order_id)
    demo.ok(f"two 503s, then success: answered {timeline}; handed over as {created}")

    failing = sample(Kind.VALID)
    [message] = failing.messages
    demo.stack.add_fault(failing.order_id, FAULT_STATUS, times=2 * MAX_ATTEMPTS)
    demo.publish(failing)
    demo.outcomes(failing.messages, ["dead_letter.written (retries-exhausted)"])
    stored_as(demo, failing.order_id, "handover_failed")
    letter, _ = dead_letter_of(demo, message.trace_id)
    attempts = letter.headers.get("dlt.attempts")
    demo.check(attempts == str(MAX_ATTEMPTS), f"{MAX_ATTEMPTS} attempts", attempts)
    requests = demo.stack.handover_requests(failing.order_id)
    took = (requests[-1].received_at - requests[0].received_at).total_seconds()
    nothing_handed_over = demo.stack.fulfilment_orders(failing.order_id)
    demo.check(not nothing_handed_over, "no fulfilment order", nothing_handed_over)
    demo.ok(
        f"always 503: {attempts} attempts over {took:.1f} s (backoff with full jitter), "
        "then handover_failed and a retries-exhausted dead letter"
    )

    demo.stack.clear_faults(failing.order_id)
    demo.ok("fault rule cleared: the fulfilment service is back for this order")
    arguments = ["--order-id", str(failing.order_id), "--execute"]
    demo.show(f'$ make replay ARGS="{" ".join(arguments)}"')
    code, output = demo.stack.replay(arguments)
    for line in output:
        demo.show(f"  {line}")
    demo.check(code == 0, "the replay command to succeed", code)
    demo.replayed(message)
    demo.outcomes(
        failing.messages, ["dead_letter.written (retries-exhausted)", "handover.completed"]
    )
    stored = stored_as(demo, failing.order_id, "handed_over")
    created = one_fulfilment_order(demo, failing.order_id)
    demo.check(stored.trace_id == message.trace_id, "the original trace", stored.trace_id)
    demo.ok(f"replayed and handed over as {created}, in the same trace {message.trace_id}")
    demo.follow(failing.order_id, message.trace_id)


SCENARIO = (
    Section(
        "1. Successful processing",
        "tests/integration/test_successful_processing.py",
        successful_processing,
    ),
    Section("2. Invalid messages", "tests/integration/test_invalid_messages.py", invalid_messages),
    Section("3. Duplicate events", "tests/integration/test_duplicate_events.py", duplicate_events),
    Section(
        "4. Downstream API failures",
        "tests/integration/test_downstream_failures.py",
        downstream_failures,
    ),
)
