"""The order placed event: a CloudEvent in structured mode carrying the order (ADR 0001)."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class OrderPlacedEvent:
    event_id: UUID
    data: Mapping[str, Any]


def parse_order_placed(value: bytes) -> OrderPlacedEvent:
    """Reads the envelope. Validation of every layer is added in front of this (ADR 0001)."""
    envelope = json.loads(value)
    return OrderPlacedEvent(event_id=UUID(envelope["id"]), data=envelope["data"])
