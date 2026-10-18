"""Layer 5: the business rules a schema can't express (ADR 0001).

They run on data that has passed the schema, and report every rule that is broken. Nothing is
corrected: a wrong value makes the event invalid.
"""

from collections.abc import Mapping
from typing import Any

from order_consumer.domain.money import CURRENCY_EXPONENTS
from order_consumer.domain.validation import FieldError, quote


def business_rule_errors(key: bytes | None, data: Mapping[str, Any]) -> list[FieldError]:
    return [
        *_key_is_the_order_id(key, data["orderId"]),
        *_currency_is_allowed(data["currency"]),
        *_skus_are_unique(data["lines"]),
        *_total_is_the_sum_of_the_lines(data["lines"], data["total"]),
    ]


def _key_is_the_order_id(key: bytes | None, order_id: str) -> list[FieldError]:
    """Compared as bytes: the key decides the partition, so the same UUID spelt differently would
    put the order on another partition."""
    if key == order_id.encode():
        return []
    if key is None:
        message = "differs from the Kafka key: the record has none"
    else:
        message = f"differs from the Kafka key {quote(key.decode('utf-8', 'backslashreplace'))}"
    return [FieldError("/data/orderId", message)]


def _currency_is_allowed(currency: str) -> list[FieldError]:
    if currency in CURRENCY_EXPONENTS:
        return []
    return [FieldError("/data/currency", f"{quote(currency)} is not an allowed currency")]


def _skus_are_unique(lines: list[Mapping[str, Any]]) -> list[FieldError]:
    seen: set[str] = set()
    errors = []
    for index, line in enumerate(lines):
        sku = line["sku"]
        if sku in seen:
            errors.append(
                FieldError(
                    f"/data/lines/{index}/sku", f"{quote(sku)} appears in more than one line"
                )
            )
        seen.add(sku)
    return errors


def _total_is_the_sum_of_the_lines(lines: list[Mapping[str, Any]], total: int) -> list[FieldError]:
    expected = sum(line["quantity"] * line["unitPrice"] for line in lines)
    if total == expected:
        return []
    return [
        FieldError("/data/total", f"{quote(total)} is not the sum of the line totals, {expected}")
    ]
