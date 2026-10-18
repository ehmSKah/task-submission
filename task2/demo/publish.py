"""`make publish`: sample order placed events onto the stack's order topic (ADR 0009).

    python -m demo.publish [KIND]... [--count N] [--currency C]

Each sample is a new order with its own trace. One line per record says what it is, its order,
event and trace ids, and where it went, so it can be followed in kafka-ui, PostgreSQL, the logs and
Jaeger. Bodies and customer ids are never printed (ADR 0008).
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from demo.samples import Kind, Message, sample
from order_consumer.domain.money import CURRENCY_EXPONENTS

Send = Callable[[Sequence[Message]], list[str]]
"""Publishes the messages in order and returns each one's position, e.g. orders/1/42."""

CURRENCIES = sorted(CURRENCY_EXPONENTS)  # the consumer's allow-list (ADR 0001)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish",
        description="Publish sample order placed events, each a new order with its own "
        "traceparent. Kinds: valid; an invalid event per validation layer (unreadable, "
        "not-a-cloudevent, unsupported-type-or-version, schema-violation, business-rule); "
        "redelivered (the same record twice), re-emitted (the order again under a new event "
        "id), conflicting (the order again with other lines and total).",
    )
    parser.add_argument(
        "kinds",
        nargs="*",
        metavar="KIND",
        choices=[kind.value for kind in Kind],
        help="what to publish (default: valid)",
    )
    parser.add_argument("--count", type=int, default=1, help="samples of each kind (default: 1)")
    parser.add_argument(
        "--currency", choices=CURRENCIES, default="EUR", help="of valid events (default: EUR)"
    )
    return parser


def run(argv: Sequence[str], send: Send, out: TextIO = sys.stdout) -> int:
    arguments = _parser().parse_args(argv)
    for kind in arguments.kinds or [Kind.VALID.value]:
        for _ in range(arguments.count):
            published = sample(Kind(kind), currency=arguments.currency)
            positions = send(published.messages)
            for message, position in zip(published.messages, positions, strict=True):
                print(
                    f"{kind:<27} order {published.order_id}  event {message.event_id or '-'}  "
                    f"trace {message.trace_id}  {position}",
                    file=out,
                )
    return 0


def main() -> int:
    from demo.stack import Endpoints, Stack

    return run(sys.argv[1:], Stack(Endpoints.from_env()).send)


if __name__ == "__main__":
    raise SystemExit(main())
