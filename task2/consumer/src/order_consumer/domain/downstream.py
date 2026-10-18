"""Mapping 2: an Order becomes the fulfilment service's request (ADR 0001, 0003).

The request has the fulfilment service's own shape on purpose, so a change on either side touches
only one mapping.
"""

from dataclasses import dataclass
from typing import Any

from order_consumer.domain.money import decimal_amount
from order_consumer.domain.order import Order


@dataclass(frozen=True, slots=True)
class DownstreamRequest:
    idempotency_key: str  # the order id (ADR 0002)
    payload: dict[str, Any]  # the JSON body, in the fulfilment contract's shape


def to_downstream_request(order: Order) -> DownstreamRequest:
    def amount(minor_units: int) -> dict[str, str]:
        return {"amount": decimal_amount(minor_units, order.currency), "currency": order.currency}

    return DownstreamRequest(
        idempotency_key=str(order.order_id),
        payload={
            "externalReference": str(order.order_id),
            "customerReference": str(order.customer_id),
            "orderedAt": order.placed_at.isoformat().replace("+00:00", "Z"),
            "lines": [
                {
                    "sku": line.sku,
                    "quantity": line.quantity,
                    "unitPrice": amount(line.unit_price),
                    "lineTotal": amount(line.line_total),
                }
                for line in order.lines
            ],
            "total": amount(order.total),
        },
    )
