"""What makes an event invalid, and how its errors are reported (ADR 0001, 0008)."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

MAX_ERRORS = 20
MAX_VALUE_LENGTH = 64


class Layer(Enum):
    """The validation layers, in the order they are checked (ADR 0001)."""

    UNREADABLE = "unreadable"
    NOT_A_CLOUDEVENT = "not-a-cloudevent"
    UNSUPPORTED_TYPE_OR_VERSION = "unsupported-type-or-version"
    SCHEMA_VIOLATION = "schema-violation"
    BUSINESS_RULE = "business-rule"


@dataclass(frozen=True, slots=True)
class FieldError:
    pointer: str  # a JSON Pointer into the event; "" is the whole event
    message: str


class InvalidEvent(Exception):
    """The event can never be processed as sent. Being invalid is permanent (ADR 0001).

    Every error of the failing layer is counted; the first 20 are kept. The event and order ids
    are given when they could be read.
    """

    def __init__(
        self,
        layer: Layer,
        errors: Sequence[FieldError],
        event_id: UUID | None = None,
        order_id: UUID | None = None,
    ) -> None:
        super().__init__(f"invalid event ({layer.value}, {len(errors)} errors)")
        self.layer = layer
        self.errors = tuple(errors[:MAX_ERRORS])
        self.error_count = len(errors)
        self.event_id = event_id
        self.order_id = order_id


def quote(value: object) -> str:
    """A value as validation messages quote it, cut to 64 characters (ADR 0008)."""
    return cut(repr(value))


def cut(text: str) -> str:
    return text if len(text) <= MAX_VALUE_LENGTH else text[: MAX_VALUE_LENGTH - 1] + "…"
