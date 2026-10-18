"""Money in integer minor units, and the currencies orders may use (ADR 0001)."""

from decimal import Decimal

# The allow-list: each currency with its exponent, the number of minor-unit digits.
CURRENCY_EXPONENTS: dict[str, int] = {"EUR": 2, "USD": 2, "GBP": 2, "JPY": 0, "KWD": 3}


def decimal_amount(minor_units: int, currency: str) -> str:
    """An amount in minor units as a decimal string, e.g. 1999 EUR -> "19.99"."""
    return str(Decimal(minor_units).scaleb(-CURRENCY_EXPONENTS[currency]))
