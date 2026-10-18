"""The replay command: feeds dead letters back into processing once their cause is fixed (ADR 0006).

    python -m order_consumer.replay [--reason R]... [--order-id ID]... [--since T] [--until T]
                                    [--force] [--execute]

A dry run unless `--execute`: it lists every dead letter the filters select and what it would do
with it. `make replay ARGS="…"` runs it in the consumer image, against the stack's topics.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TextIO
from uuid import UUID

from order_consumer.adapters.inbound.dead_letter_reader import read_dead_letters
from order_consumer.adapters.outbound.kafka_republisher import KafkaRepublisher
from order_consumer.application.ports import DeadLetterReason
from order_consumer.application.replay import DeadLetterRecord, Replay, Selection, Skipped, decide
from order_consumer.config import ConfigurationError

EXIT_DONE = 0
EXIT_FAILED = 1  # a record was not republished, or the dead letters could not be read
EXIT_CONFIGURATION = 2  # also argparse's exit code for invalid arguments


@dataclass(frozen=True, slots=True)
class ReplaySettings:
    kafka_bootstrap_servers: str
    kafka_topic: str = "orders"
    kafka_dead_letter_topic: str = "orders.dlt"
    kafka_group_id: str = "order-replay"  # its own group, never the consumers'

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ReplaySettings":
        bootstrap = env.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not bootstrap:
            raise ConfigurationError("KAFKA_BOOTSTRAP_SERVERS is not set")
        return cls(
            kafka_bootstrap_servers=bootstrap,
            kafka_topic=env.get("KAFKA_TOPIC", "orders"),
            kafka_dead_letter_topic=env.get("KAFKA_DEAD_LETTER_TOPIC", "orders.dlt"),
        )


def _moment(value: str) -> datetime:
    """ISO 8601; without an offset, UTC."""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replay",
        description="Republish dead letters to the order topic once their cause is fixed. "
        "A dry run unless --execute. --reason and --order-id may be given several times; "
        "a dead letter is selected when it matches every kind of filter given.",
    )
    parser.add_argument(
        "--reason",
        action="append",
        default=[],
        choices=[reason.value for reason in DeadLetterReason],
        help="only dead letters with this dlt.reason",
    )
    parser.add_argument(
        "--order-id", action="append", default=[], type=UUID, help="only this order's dead letters"
    )
    parser.add_argument(
        "--since",
        type=_moment,
        help="only dead letters written at or after, e.g. 2026-10-18T09:00Z",
    )
    parser.add_argument("--until", type=_moment, help="only dead letters written before")
    parser.add_argument(
        "--force",
        action="store_true",
        help="also replay invalid events (other than an unsupported type or version) and "
        "conflicting events, whose unchanged bytes fail again",
    )
    parser.add_argument("--execute", action="store_true", help="publish; without it, a dry run")
    return parser


def _line(dead_letter: DeadLetterRecord, status: str) -> str:
    reason = dead_letter.header("dlt.reason") or "-"
    if dead_letter.reason is DeadLetterReason.INVALID and dead_letter.layer is not None:
        reason = f"{reason} ({dead_letter.layer.value})"
    order_id = dead_letter.order_id
    written = (
        "-"
        if dead_letter.timestamp is None
        else dead_letter.timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return f"{dead_letter.position}  {reason}  order {order_id or '-'}  {written}  {status}"


def run(argv: Sequence[str], settings: ReplaySettings, out: TextIO = sys.stdout) -> int:
    arguments = _parser().parse_args(argv)
    selection = Selection(
        reasons=frozenset(DeadLetterReason(reason) for reason in arguments.reason),
        order_ids=frozenset(arguments.order_id),
        since=arguments.since,
        until=arguments.until,
        force=arguments.force,
    )
    decided: list[tuple[DeadLetterRecord, Replay | Skipped]] = []
    for dead_letter in read_dead_letters(
        settings.kafka_bootstrap_servers,
        settings.kafka_dead_letter_topic,
        settings.kafka_group_id,
    ):
        decision = decide(dead_letter, selection)
        if decision is not None:
            decided.append((dead_letter, decision))
    replays = [
        (dead_letter, decision.headers)
        for dead_letter, decision in decided
        if isinstance(decision, Replay)
    ]
    skipped = len(decided) - len(replays)

    failed: dict[str, str] = {}
    if arguments.execute and replays:
        republisher = KafkaRepublisher(settings.kafka_bootstrap_servers, settings.kafka_topic)
        failed = republisher.republish(replays)

    for dead_letter, decision in decided:
        if isinstance(decision, Skipped):
            status = f"skipped: {decision.why}"
        elif not arguments.execute:
            status = "would replay"
        elif dead_letter.position in failed:
            status = f"failed: {failed[dead_letter.position]}"
        else:
            status = "replayed"
        print(_line(dead_letter, status), file=out)

    topic = settings.kafka_topic
    if not arguments.execute:
        counts = f"{len(replays)} to replay, {skipped} skipped"
        print(f"dry run: {counts}; --execute publishes them to {topic}", file=out)
        return EXIT_DONE
    summary = f"replayed {len(replays) - len(failed)} to {topic}, {skipped} skipped"
    print(f"{summary}, {len(failed)} failed" if failed else summary, file=out)
    return EXIT_FAILED if failed else EXIT_DONE


def main() -> int:
    try:
        settings = ReplaySettings.from_env()
    except ConfigurationError as error:
        print(f"replay: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    try:
        return run(sys.argv[1:], settings)
    except Exception as error:
        print(f"replay failed: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
