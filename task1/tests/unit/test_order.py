from orders_api.domain.order import LineItem
from tests.builders import an_order


def test_total_is_the_sum_of_quantity_times_unit_price() -> None:
    order = an_order(
        items=(
            LineItem(sku="ABC-123", quantity=2, unit_price=1999),
            LineItem(sku="XYZ-9", quantity=1, unit_price=500),
        )
    )

    assert order.total_amount == 4498
