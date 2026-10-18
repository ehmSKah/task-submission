"""Sample order placed events for `make publish` and `make demo` (ADR 0001, 0009).

Each sample is a new order, made of the Kafka records to publish for it: one event, or two for the
duplicate kinds. Every record is keyed by its order id and carries a W3C `traceparent` starting a
trace of its own (ADR 0008), so its log lines, spans and dead letter can be found by that id.
"""

import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

# Made up, and never printed: a customer id is personal data (ADR 0008).
CUSTOMER_ID = "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c"
LINES = [
    {"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1999},
    {"sku": "KNE-ELV-0007", "quantity": 1, "unitPrice": 500},
]
TOTAL = 4498  # in minor units: 2 * 1999 + 500


class Kind(StrEnum):
    VALID = "valid"
    # One per validation layer, named after it (ADR 0001).
    UNREADABLE = "unreadable"
    NOT_A_CLOUDEVENT = "not-a-cloudevent"
    UNSUPPORTED_TYPE_OR_VERSION = "unsupported-type-or-version"
    SCHEMA_VIOLATION = "schema-violation"
    BUSINESS_RULE = "business-rule"
    # Duplicate and conflicting events (ADR 0002).
    REDELIVERED = "redelivered"
    RE_EMITTED = "re-emitted"
    CONFLICTING = "conflicting"


INVALID_KINDS = (
    Kind.UNREADABLE,
    Kind.NOT_A_CLOUDEVENT,
    Kind.UNSUPPORTED_TYPE_OR_VERSION,
    Kind.SCHEMA_VIOLATION,
    Kind.BUSINESS_RULE,
)


@dataclass(frozen=True, slots=True)
class Message:
    """One Kafka record."""

    key: bytes
    value: bytes
    headers: tuple[tuple[str, bytes], ...]
    trace_id: str
    event_id: UUID | None  # None where the event has no readable id


@dataclass(frozen=True, slots=True)
class Sample:
    kind: Kind
    order_id: UUID
    messages: tuple[Message, ...]


def envelope(order_id: UUID, event_id: UUID, currency: str = "EUR", **data: Any) -> dict[str, Any]:
    """A valid event for the order; keyword arguments override fields of its data."""
    return {
        "specversion": "1.0",
        "id": str(event_id),
        "source": "/orders",
        "type": "order.placed",
        "time": "2026-10-17T09:12:04Z",
        "dataschema": "urn:task2:schema:order-placed:1",
        "datacontenttype": "application/json",
        "data": {
            "orderId": str(order_id),
            "customerId": CUSTOMER_ID,
            "placedAt": "2026-10-17T11:12:03+02:00",
            "currency": currency,
            "lines": LINES,
            "total": TOTAL,
        }
        | data,
    }


def _message(order_id: UUID, value: dict[str, Any] | bytes, event_id: UUID | None) -> Message:
    trace_id = secrets.token_hex(16)
    traceparent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    return Message(
        key=str(order_id).encode(),
        value=value if isinstance(value, bytes) else json.dumps(value).encode(),
        headers=(("traceparent", traceparent.encode()),),
        trace_id=trace_id,
        event_id=event_id,
    )


def sample(kind: Kind, currency: str = "EUR") -> Sample:
    """A new order of this kind; `currency` applies to valid ones."""
    order_id, event_id = uuid4(), uuid4()

    def event(under: UUID = event_id, **changes: Any) -> Message:
        """The order's event under this event id; keyword arguments change its data."""
        return _message(order_id, envelope(order_id, under, **changes), under)

    match kind:
        case Kind.VALID:
            messages = [event(currency=currency)]
        case Kind.UNREADABLE:
            # A record broken off after 40 bytes.
            truncated = json.dumps(envelope(order_id, event_id)).encode()[:40]
            messages = [_message(order_id, truncated, None)]
        case Kind.NOT_A_CLOUDEVENT:
            bare = envelope(order_id, event_id)
            del bare["specversion"], bare["id"]
            messages = [_message(order_id, bare, None)]
        case Kind.UNSUPPORTED_TYPE_OR_VERSION:
            messages = [
                _message(
                    order_id, envelope(order_id, event_id) | {"type": "order.cancelled"}, event_id
                )
            ]
        case Kind.SCHEMA_VIOLATION:
            messages = [event(lines=[{"sku": "KNE-ESC-0042", "quantity": 0, "unitPrice": 1999}])]
        case Kind.BUSINESS_RULE:
            messages = [event(total=TOTAL + 1)]
        case Kind.REDELIVERED:
            # Kafka handing out the same record again, e.g. after a crash before the commit.
            messages = [event()] * 2
        case Kind.RE_EMITTED:
            # The producer sending the order again as a new event.
            messages = [event(), event(uuid4())]
        case Kind.CONFLICTING:
            # The same order again, with other content: a line missing.
            messages = [event(), event(uuid4(), lines=LINES[:1], total=2 * 1999)]
    return Sample(kind, order_id, tuple(messages))
