"""The ports the processing use case depends on (ADR 0005)."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from order_consumer.domain.downstream import DownstreamRequest
from order_consumer.domain.order import Order


@dataclass(frozen=True, slots=True)
class HandoverResult:
    fulfilment_order_id: str


class OrderStore(Protocol):
    def store_new(self, order: Order, event_id: UUID) -> None:
        """Stores the order and its lines, awaiting handover, in one transaction."""

    def record_handover(self, order_id: UUID, fulfilment_order_id: str) -> None:
        """Marks the order as handed over to the fulfilment service."""


class FulfilmentClient(Protocol):
    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        """Creates the fulfilment order, keyed by the request's idempotency key."""
