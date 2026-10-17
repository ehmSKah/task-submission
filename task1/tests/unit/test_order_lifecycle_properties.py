"""Property tests of the lifecycle (ADRs 0004 and 0011): random sequences of requested status
changes never produce an invalid status, a skipped step or a change after a final status.

The oracle below is written from ADR 0004's diagram, independently of `NEXT_STATUSES`.
"""

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from orders_api.domain.order import Order, OrderStatus, StatusChangeNotAllowed
from tests.builders import an_order

START = datetime(2026, 10, 17, 9, 30, tzinfo=UTC)
FORWARD = [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.SHIPPED, OrderStatus.DELIVERED]

requests = st.lists(st.sampled_from(OrderStatus), max_size=30)


def is_one_step(current: OrderStatus, requested: OrderStatus) -> bool:
    """One step along the forward path, or a cancellation before shipping."""
    if requested is OrderStatus.CANCELLED:
        return current in {OrderStatus.PENDING, OrderStatus.CONFIRMED}
    if current is OrderStatus.CANCELLED:
        return False
    return FORWARD.index(requested) == FORWARD.index(current) + 1


def apply_all(order: Order, requested: list[OrderStatus]) -> list[Order]:
    """The order after each request, a refused request leaving it as it was."""
    history = [order]
    for minute, status in enumerate(requested, start=1):
        with suppress(StatusChangeNotAllowed):
            order = order.with_status(status, now=START + timedelta(minutes=minute))
        history.append(order)
    return history


@given(requests)
def test_every_change_is_one_allowed_step_and_bumps_the_version_by_one(
    requested: list[OrderStatus],
) -> None:
    history = apply_all(an_order(), requested)

    for before, after in pairwise(history):
        assert isinstance(after.status, OrderStatus)
        if after != before:
            assert is_one_step(before.status, after.status)
            assert after.version == before.version + 1
            assert after.updated_at > before.updated_at
            assert (after.id, after.owner_id, after.items) == (
                before.id,
                before.owner_id,
                before.items,
            )


@given(requests)
def test_nothing_changes_after_a_final_status(requested: list[OrderStatus]) -> None:
    history = apply_all(an_order(), requested)

    final_at = next(
        (
            index
            for index, order in enumerate(history)
            if order.status in {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
        ),
        None,
    )
    if final_at is not None:
        assert all(order == history[final_at] for order in history[final_at:])


@given(requests)
def test_the_statuses_visited_are_the_forward_path_possibly_cut_short_by_cancelling(
    requested: list[OrderStatus],
) -> None:
    history = apply_all(an_order(), requested)

    visited = [
        order.status
        for index, order in enumerate(history)
        if index == 0 or order != history[index - 1]
    ]
    forward = [status for status in visited if status is not OrderStatus.CANCELLED]
    assert forward == FORWARD[: len(forward)]
    assert OrderStatus.CANCELLED not in visited[:-1]
    if visited[-1] is OrderStatus.CANCELLED:
        assert len(forward) <= 2


@given(st.sampled_from(OrderStatus), st.sampled_from(OrderStatus))
def test_a_request_is_refused_exactly_when_it_is_neither_a_step_nor_a_no_op(
    current: OrderStatus, requested: OrderStatus
) -> None:
    order = an_order(status=current)

    try:
        changed = order.with_status(requested, now=START)
    except StatusChangeNotAllowed:
        assert requested is not current
        assert not is_one_step(current, requested)
    else:
        assert changed.status is requested
        assert changed is order or is_one_step(current, requested)
