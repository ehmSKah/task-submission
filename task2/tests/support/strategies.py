"""Hypothesis strategies for valid order placed events, within ADR 0001's indicative limits."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from hypothesis import strategies as st

from order_consumer.domain.money import CURRENCY_EXPONENTS

skus = st.from_regex(r"[A-Z0-9]{1,8}(-[A-Z0-9]{1,8}){0,3}", fullmatch=True)

order_lines = st.lists(
    st.fixed_dictionaries(
        {
            "sku": skus,
            "quantity": st.integers(min_value=1, max_value=999),
            "unitPrice": st.integers(min_value=0, max_value=1_000_000),
        }
    ),
    min_size=1,
    max_size=50,
    unique_by=lambda line: line["sku"],
)

# RFC 3339 timestamps with an offset of up to ±14 hours, to the minute.
timestamps = st.builds(
    lambda moment, offset: moment.astimezone(timezone(timedelta(minutes=offset))).isoformat(),
    st.datetimes(
        min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
    ),
    st.integers(min_value=-14 * 60, max_value=14 * 60),
)


@st.composite
def order_data(draw: st.DrawFn) -> dict[str, Any]:
    lines = draw(order_lines)
    return {
        "orderId": str(draw(st.uuids())),
        "customerId": str(draw(st.uuids())),
        "placedAt": draw(timestamps),
        "currency": draw(st.sampled_from(sorted(CURRENCY_EXPONENTS))),
        "lines": lines,
        "total": sum(line["quantity"] * line["unitPrice"] for line in lines),
    }
