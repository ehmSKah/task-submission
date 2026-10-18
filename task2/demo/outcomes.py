"""Outcomes counted from the consumers' log lines (ADR 0008).

Every message ends in exactly one terminal line: `handover.completed`, `order.duplicate` or
`dead_letter.written`. Filtered by the trace ids the demo started, they are its own outcomes,
whatever else the stack has processed.
"""

import json
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass

TERMINAL_EVENTS = ("handover.completed", "order.duplicate", "dead_letter.written")


@dataclass(frozen=True, slots=True)
class TerminalLine:
    event: str
    trace_id: str
    order_id: str | None
    detail: str | None  # the duplicate's kind or the dead letter's reason


def terminal_lines(lines: Iterable[str], trace_ids: Collection[str]) -> list[TerminalLine]:
    """The terminal lines of these traces, in the order they were logged; anything that isn't a
    JSON log line is skipped."""
    found = []
    for text in lines:
        try:
            entry = json.loads(text)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("event") in TERMINAL_EVENTS and entry.get("trace_id") in trace_ids:
            found.append(
                TerminalLine(
                    entry["event"],
                    entry["trace_id"],
                    entry.get("order_id"),
                    entry.get("duplicate") or entry.get("reason"),
                )
            )
    return found


def outcome_counts(lines: Iterable[TerminalLine]) -> Counter[str]:
    """Per terminal event, with the duplicate's kind or the dead letter's reason."""
    return Counter(
        line.event if line.detail is None else f"{line.event} ({line.detail})" for line in lines
    )
