"""Which dead letters a replay republishes, and how (ADR 0006).

A replay feeds a dead letter back into processing once its cause is fixed: the original record's
value, key and headers go back to the topic it came from, without the `dlt.*` headers and with a
`replayed-from` header naming the dead letter. Processing is idempotent, so replaying a dead
letter again, or both copies of one written twice, does no harm.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from order_consumer.application.ports import DeadLetterReason
from order_consumer.domain.validation import Layer

REPLAYED_FROM = "replayed-from"
FAILURE_HEADER_PREFIX = "dlt."

Headers = tuple[tuple[str, bytes | None], ...]


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    """A record read from the dead-letter topic: the original record plus its failure headers."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None
    headers: Headers
    timestamp: datetime | None  # when it was dead-lettered, UTC

    @property
    def position(self) -> str:
        """Where the dead letter is, e.g. orders.dlt/1/42."""
        return f"{self.topic}/{self.partition}/{self.offset}"

    def header(self, name: str) -> str | None:
        """The failure header's value, if it is there and readable."""
        for key, value in self.headers:
            if key == name and value is not None:
                try:
                    return value.decode()
                except UnicodeDecodeError:
                    return None
        return None

    @property
    def reason(self) -> DeadLetterReason | None:
        try:
            return DeadLetterReason(self.header("dlt.reason"))
        except ValueError:
            return None

    @property
    def order_id(self) -> UUID | None:
        """From its failure header, or else its key: an unreadable event's key may still be it."""
        for candidate in (self.header("dlt.order-id"), self._key()):
            try:
                return UUID(candidate or "")
            except ValueError:
                continue
        return None

    def _key(self) -> str | None:
        try:
            return None if self.key is None else self.key.decode()
        except UnicodeDecodeError:
            return None

    @property
    def layer(self) -> Layer | None:
        """The validation layer an invalid event failed, from its detail."""
        try:
            detail = json.loads(self.header("dlt.detail") or "")
            return Layer(detail["layer"])
        except (ValueError, TypeError, KeyError):
            return None


@dataclass(frozen=True, slots=True)
class Selection:
    """What to replay: each filter left empty selects every dead letter."""

    reasons: frozenset[DeadLetterReason] = frozenset()
    order_ids: frozenset[UUID] = frozenset()
    since: datetime | None = None  # dead-lettered at or after
    until: datetime | None = None  # dead-lettered before
    force: bool = False  # also the dead letters whose unchanged bytes fail again

    def selects(self, dead_letter: DeadLetterRecord) -> bool:
        written = dead_letter.timestamp
        return (
            (not self.reasons or dead_letter.reason in self.reasons)
            and (not self.order_ids or dead_letter.order_id in self.order_ids)
            and (self.since is None or (written is not None and written >= self.since))
            and (self.until is None or (written is not None and written < self.until))
        )


@dataclass(frozen=True, slots=True)
class Replay:
    headers: Headers  # the original record's, and replayed-from


@dataclass(frozen=True, slots=True)
class Held:
    """Selected, but not replayed."""

    why: str


def _fails_again(dead_letter: DeadLetterRecord) -> bool:
    """Only a corrected event from the producer resolves it: the same bytes fail the same way.
    An unsupported type or version is the exception, once the consumer supports it."""
    match dead_letter.reason:
        case DeadLetterReason.CONFLICT:
            return True
        case DeadLetterReason.INVALID:
            return dead_letter.layer is not Layer.UNSUPPORTED_TYPE_OR_VERSION
        case _:
            return False


def _left_out(name: str) -> bool:
    """Not republished: a failure header, or the replayed-from of an earlier replay, which the
    new one replaces."""
    lowered = name.lower()
    return lowered.startswith(FAILURE_HEADER_PREFIX) or lowered == REPLAYED_FROM


def decide(dead_letter: DeadLetterRecord, selection: Selection) -> Replay | Held | None:
    """None when the selection leaves the dead letter out."""
    if not selection.selects(dead_letter):
        return None
    if dead_letter.reason is None:
        return Held("no known dlt.reason: not a dead letter of this consumer")
    if _fails_again(dead_letter) and not selection.force:
        return Held("unchanged bytes fail again; --force replays it")
    original = tuple((name, value) for name, value in dead_letter.headers if not _left_out(name))
    return Replay(headers=(*original, (REPLAYED_FROM, dead_letter.position.encode())))
