"""The ports the processing use case depends on (ADR 0005)."""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol
from uuid import UUID

from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.order import Order


@dataclass(frozen=True, slots=True)
class HandoverResult:
    fulfilment_order_id: str


class HandoverStatus(Enum):
    """How far a stored order's handover got (ADR 0004). Anything but handed over is unfinished."""

    AWAITING_HANDOVER = "awaiting_handover"
    HANDED_OVER = "handed_over"
    HANDOVER_FAILED = "handover_failed"


class OrderStore(Protocol):
    def store_new(self, order: Order, event_id: UUID) -> bool:
        """Stores the order and its lines, awaiting handover, in one transaction.

        Returns False, writing nothing, when an order with this id is already stored.
        """

    def handover_status(self, order_id: UUID) -> HandoverStatus:
        """The handover status of a stored order."""

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        """Marks the order as handed over to the fulfilment service."""


class FulfilmentClient(Protocol):
    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        """Creates the fulfilment order, keyed by the request's idempotency key."""
