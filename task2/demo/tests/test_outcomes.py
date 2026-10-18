"""The demo counts outcomes from the consumers' terminal log lines (ADR 0008): exactly one per
message, `handover.completed`, `order.duplicate` or `dead_letter.written`."""

import json
from typing import Any

from demo.outcomes import TerminalLine, outcome_counts, terminal_lines

OURS, ALSO_OURS, ANOTHER_RUN = "a" * 32, "b" * 32, "c" * 32


def line(event: str, trace_id: str | None = OURS, **fields: Any) -> str:
    entry = {"event": event, "service": "order-consumer", "level": "info", **fields}
    if trace_id is not None:
        entry["trace_id"] = trace_id
    return json.dumps(entry)


def test_only_the_terminal_lines_of_the_given_traces_are_kept_in_order() -> None:
    logs = [
        line("consumer.started", trace_id=None),
        line("event.received", order_id="o-1"),
        line("handover.completed", order_id="o-1", fulfilment_order_id="ful-7", attempts=1),
        line("handover.completed", trace_id=ANOTHER_RUN, order_id="o-9"),
        line("order.duplicate", trace_id=ALSO_OURS, order_id="o-2", duplicate="re-emission"),
        line("dead_letter.written", trace_id=ALSO_OURS, reason="invalid"),
    ]

    assert terminal_lines(logs, {OURS, ALSO_OURS}) == [
        TerminalLine("handover.completed", OURS, "o-1", None),
        TerminalLine("order.duplicate", ALSO_OURS, "o-2", "re-emission"),
        TerminalLine("dead_letter.written", ALSO_OURS, None, "invalid"),
    ]


def test_lines_that_are_not_json_objects_are_skipped() -> None:
    logs = [
        "",
        "Traceback (most recent call last):",
        "42",
        '["handover.completed"]',
        '{"event": "handover.completed", "trace_id": ',
        line("handover.completed", order_id="o-1"),
    ]

    assert terminal_lines(logs, {OURS}) == [TerminalLine("handover.completed", OURS, "o-1", None)]


def test_outcomes_are_counted_by_event_with_the_duplicate_kind_or_dead_letter_reason() -> None:
    lines = [
        TerminalLine("dead_letter.written", OURS, None, "invalid"),
        TerminalLine("handover.completed", OURS, "o-1", None),
        TerminalLine("order.duplicate", OURS, "o-2", "redelivery"),
        TerminalLine("handover.completed", OURS, "o-2", None),
        TerminalLine("dead_letter.written", OURS, "o-3", "invalid"),
        TerminalLine("dead_letter.written", OURS, "o-4", "conflict"),
    ]

    assert outcome_counts(lines) == {
        "handover.completed": 2,
        "order.duplicate (redelivery)": 1,
        "dead_letter.written (invalid)": 2,
        "dead_letter.written (conflict)": 1,
    }


def test_outcomes_are_listed_in_the_order_of_the_event_catalogue() -> None:
    lines = [
        TerminalLine("dead_letter.written", OURS, "o-3", "invalid"),
        TerminalLine("order.duplicate", OURS, "o-2", "redelivery"),
        TerminalLine("dead_letter.written", OURS, "o-4", "conflict"),
        TerminalLine("handover.completed", OURS, "o-1", None),
    ]

    assert list(outcome_counts(lines)) == [
        "handover.completed",
        "order.duplicate (redelivery)",
        "dead_letter.written (conflict)",
        "dead_letter.written (invalid)",
    ]
