"""The published JSON shape of an Order (ADR 0004; schema `Order` in openapi.yaml)."""

from typing import Any

from orders_api.domain.order import Order


def order_to_json(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "ownerId": order.owner_id,
        "status": order.status.value,
        "currency": order.currency,
        "items": [
            {"sku": item.sku, "quantity": item.quantity, "unitPrice": item.unit_price}
            for item in order.items
        ],
        "totalAmount": order.total_amount,
        "createdAt": order.created_at,
        "updatedAt": order.updated_at,
        "version": order.version,
    }
