import re
from datetime import UTC, datetime, timedelta

from orders_api.adapters.outbound.system import RandomOrderIds, SystemClock
from orders_api.contract import path_parameter_schema

ID_PATTERN = path_parameter_schema("/api/v1/orders/{id}", "id")["pattern"]


def test_order_ids_are_lowercase_uuid_v4_matching_the_contract() -> None:
    ids = RandomOrderIds()

    generated = [ids.new_id() for _ in range(200)]

    assert all(re.search(ID_PATTERN, order_id) for order_id in generated)
    assert len(set(generated)) == len(generated)


def test_system_clock_is_utc_aware_and_current() -> None:
    now = SystemClock().now()

    assert now.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - now) < timedelta(seconds=5)
