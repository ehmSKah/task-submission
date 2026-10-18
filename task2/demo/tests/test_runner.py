"""The demo's runner, without a stack: sections run in order, each ending with the test file that
proves it, then the summary; the first expectation not met stops the run and fails it."""

import json
import re
from collections.abc import Callable, Sequence
from uuid import UUID

from demo.runner import Demo, Section, run
from demo.samples import Kind, Message, sample
from demo.stack import DeadLetter, Endpoints, HandoverRequest, StoredOrder

ENDPOINTS = Endpoints(
    host="stack",
    kafka="stack:9094",
    database_url="postgresql://stack/orders",
    stub="http://stack:8081",
    kafka_ui="http://stack:8080",
    jaeger_ui="http://stack:16686",
)
QUICK = {"timeout": 0.3, "interval": 0.01}


class FakeStack:
    """A stack whose consumers end each message as `outcome` says: a terminal event and its
    detail, or None for no terminal line at all. Dead-lettered messages land on the topic."""

    endpoints = ENDPOINTS

    def __init__(self, outcome: Callable[[Message], tuple[str, str | None] | None]) -> None:
        self.outcome = outcome
        self.lines: list[str] = ["not a JSON line"]
        self.letters: list[DeadLetter] = []
        self.jaeger = False

    def send(self, messages: Sequence[Message]) -> list[str]:
        for message in messages:
            self.end(message)
        return [f"orders/0/{n}" for n in range(len(messages))]

    def end(self, message: Message) -> None:
        ended = self.outcome(message)
        if ended is None:
            return
        event, detail = ended
        field = "duplicate" if event == "order.duplicate" else "reason"
        entry = {"event": event, "trace_id": message.trace_id, field: detail}
        self.lines.append(json.dumps(entry))
        if event == "dead_letter.written":
            traceparent = f"00-{message.trace_id}-{'1' * 16}-01"
            headers = {"traceparent": traceparent, "dlt.reason": detail or ""}
            self.letters.append(DeadLetter(f"orders.dlt/0/{len(self.letters)}", headers))

    def log_lines(self) -> list[str]:
        return list(self.lines)

    def dead_letters(self) -> list[DeadLetter]:
        return list(self.letters)

    def stored_order(self, order_id: UUID) -> StoredOrder | None:
        return None

    def fulfilment_orders(self, order_id: UUID) -> list[str]:
        return []

    def handover_requests(self, order_id: UUID) -> list[HandoverRequest]:
        return []

    def add_fault(self, order_id: UUID, status: int, times: int) -> None:
        pass

    def clear_faults(self, order_id: UUID) -> None:
        pass

    def replay(self, arguments: Sequence[str]) -> tuple[int, list[str]]:
        return 0, []

    def tracing(self) -> bool:
        return self.jaeger


def handed_over(message: Message) -> tuple[str, str | None]:
    return "handover.completed", None


def valid_orders(count: int) -> Callable[[Demo], None]:
    def steps(demo: Demo) -> None:
        for _ in range(count):
            published = demo.publish(sample(Kind.VALID))
            demo.outcomes(published.messages, ["handover.completed"])
            demo.ok(f"order {str(published.order_id)[:8]}… handed over")

    return steps


def invalid_order(demo: Demo) -> None:
    published = demo.publish(sample(Kind.BUSINESS_RULE))
    demo.outcomes(published.messages, ["dead_letter.written (invalid)"])
    demo.ok("dead-lettered")


def a_wait_never_met(demo: Demo) -> None:
    demo.wait(lambda: "awaiting_handover", lambda status: status == "handed_over", "a handover")


SECTIONS = (
    Section("1. Valid", "tests/integration/test_one.py", valid_orders(2)),
    Section("2. Invalid", "tests/integration/test_two.py", invalid_order),
)


def by_kind(message: Message) -> tuple[str, str | None]:
    """Valid events are handed over, the rest dead-lettered as invalid."""
    valid = b'"total": 4498' in message.value
    return handed_over(message) if valid else ("dead_letter.written", "invalid")


def lines_of(stack: FakeStack, sections: Sequence[Section]) -> tuple[bool, list[str]]:
    out: list[str] = []
    passed = run(sections, stack, out=out.append, **QUICK)
    return passed, out


def test_sections_run_in_order_each_ending_with_its_proof_then_the_summary() -> None:
    passed, out = lines_of(FakeStack(by_kind), SECTIONS)

    assert passed is True
    assert [line for line in out if line and not line.startswith(" ")] == [
        "1. Valid",
        "2. Invalid",
        "Summary",
    ]
    assert out[: out.index("2. Invalid")] == [
        "1. Valid",
        *[line for line in out if "✓ order" in line],
        "   proven by tests/integration/test_one.py",
        "",
    ]
    assert "   proven by tests/integration/test_two.py" in out
    assert "✗" not in "\n".join(out)


def test_the_summary_counts_the_runs_terminal_lines_and_dead_letters_by_reason() -> None:
    stack = FakeStack(by_kind)
    stack.lines.append(json.dumps({"event": "handover.completed", "trace_id": "f" * 32}))

    _, out = lines_of(stack, SECTIONS)

    summary = "\n".join(out[out.index("Summary") :])
    assert "3 published + 0 replayed = 3 messages, 3 terminal log lines: one each" in summary
    assert re.search(r"handover\.completed +2\n", summary)
    assert re.search(r"dead_letter\.written \(invalid\) +1\n", summary)
    assert "dead letters of this run on orders.dlt: invalid 1" in summary


def test_the_summary_says_where_to_look_and_links_the_followed_trace_when_jaeger_runs() -> None:
    stack = FakeStack(by_kind)
    stack.jaeger = True

    def follow(demo: Demo) -> None:
        published = demo.publish(sample(Kind.VALID))
        demo.outcomes(published.messages, ["handover.completed"])
        demo.follow(published.order_id, published.messages[0].trace_id)

    _, out = lines_of(stack, [Section("1. Followed", "tests/integration/test_one.py", follow)])

    summary = "\n".join(out)
    trace_id = stack.lines[-1].split('"trace_id": "')[1][:32]
    assert "http://stack:8080/ui/clusters/task2/all-topics/orders.dlt/messages" in summary
    assert f"http://stack:16686/trace/{trace_id}" in summary
    assert "make ps" in summary


def test_without_jaeger_the_summary_says_how_to_start_it() -> None:
    _, out = lines_of(FakeStack(by_kind), SECTIONS)

    assert "make up-tracing" in "\n".join(out)


def test_a_wait_never_met_shows_what_it_last_observed_and_stops_the_run() -> None:
    sections = (Section("1. Stuck", "tests/integration/test_one.py", a_wait_never_met), *SECTIONS)

    passed, out = lines_of(FakeStack(by_kind), sections)

    assert passed is False
    assert "   ✗ timed out waiting for a handover; last observed: 'awaiting_handover'" in out
    assert "2. Invalid" not in out
    assert "Summary" not in out
    assert not any("proven by" in line for line in out)


def test_another_outcome_than_expected_fails_the_section() -> None:
    passed, out = lines_of(FakeStack(lambda message: ("dead_letter.written", "invalid")), SECTIONS)

    assert passed is False
    assert (
        "   ✗ expected handover.completed, the consumers logged dead_letter.written (invalid)"
    ) in out


def test_a_message_without_a_terminal_line_fails_the_summary() -> None:
    def silent_for_invalid(message: Message) -> tuple[str, str | None] | None:
        return handed_over(message) if b'"total": 4498' in message.value else None

    def unawaited(demo: Demo) -> None:
        demo.publish(sample(Kind.BUSINESS_RULE))

    sections = (SECTIONS[0], Section("2. Unawaited", "tests/integration/test_two.py", unawaited))

    passed, out = lines_of(FakeStack(silent_for_invalid), sections)

    assert passed is False
    assert "   ✗ 2 terminal log lines for 3 messages: every message ends in exactly one" in out


def test_replayed_messages_count_as_messages() -> None:
    stack = FakeStack(by_kind)

    def replayed(demo: Demo) -> None:
        published = demo.publish(sample(Kind.VALID))
        demo.outcomes(published.messages, ["handover.completed"])
        stack.end(published.messages[0])  # fed back by the replay command
        demo.replayed(published.messages[0])
        demo.outcomes(published.messages, ["handover.completed", "handover.completed"])

    passed, out = lines_of(stack, [Section("1. Again", "tests/integration/test_one.py", replayed)])

    assert passed is True
    assert "   1 published + 1 replayed = 2 messages, 2 terminal log lines: one each" in out
