"""Log event names are fixed and part of the contract (ADR 0008): every event the consumer or the
stub logs is in the ADR's catalogue."""

import ast
import re
from pathlib import Path

TASK2 = Path(__file__).parents[3]
ADR = TASK2 / "docs" / "adr" / "0008-observability-tracing-and-logs.md"
SOURCES = [TASK2 / "consumer" / "src", TASK2 / "fulfilment-stub" / "src"]
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}


def catalogue() -> set[str]:
    """The event names in the ADR's Events table."""
    section = ADR.read_text(encoding="utf-8").split("**Events**", 1)[1]
    table = section.split("\n\n", 2)[1]
    return set(re.findall(r"`([a-z_]+\.[a-z_]+)`", table))


def logged_events() -> dict[str, list[str]]:
    """Every event name passed to `log.<level>(...)`, with where it is logged."""
    found: dict[str, list[str]] = {}
    for root in SOURCES:
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LEVELS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "log"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    where = f"{path.relative_to(TASK2)}:{node.lineno}"
                    found.setdefault(node.args[0].value, []).append(where)
    return found


def test_the_catalogue_is_read_from_the_adr() -> None:
    assert {"event.received", "handover.completed", "request.invalid"} <= catalogue()


def test_every_logged_event_is_in_the_catalogue() -> None:
    logged = logged_events()

    assert {"event.received", "dead_letter.written", "fulfilment_order.created"} <= set(logged)
    unknown = {name: where for name, where in logged.items() if name not in catalogue()}
    assert unknown == {}, "events missing from ADR 0008's catalogue"
