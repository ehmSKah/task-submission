"""Order placed events for tests, shaped as ADR 0001 defines them."""

import json
from typing import Any
from uuid import UUID, uuid4


def order_placed_envelope(
    order_id: UUID, event_id: UUID | None = None, **data: Any
) -> dict[str, Any]:
    """A valid CloudEvent for the order, as a dict to change; keyword arguments override fields of
    its data."""
    return {
        "specversion": "1.0",
        "id": str(event_id or uuid4()),
        "source": "/orders",
        "type": "order.placed",
        "time": "2026-10-17T09:12:04Z",
        "dataschema": "urn:task2:schema:order-placed:1",
        "datacontenttype": "application/json",
        "data": {
            "orderId": str(order_id),
            "customerId": "a9e2b7c4-1d3f-4e5a-8b6c-7d8e9f0a1b2c",
            "placedAt": "2026-10-17T11:12:03+02:00",
            "currency": "EUR",
            "lines": [
                {"sku": "KNE-ESC-0042", "quantity": 2, "unitPrice": 1999},
                {"sku": "KNE-ELV-0007", "quantity": 1, "unitPrice": 500},
            ],
            "total": 4498,
        }
        | data,
    }


def order_placed_event(order_id: UUID, event_id: UUID | None = None, **data: Any) -> bytes:
    """A valid CloudEvent for the order; keyword arguments override fields of its data."""
    return json.dumps(order_placed_envelope(order_id, event_id, **data)).encode()
