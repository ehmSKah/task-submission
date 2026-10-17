"""Status changes as a use case: permission, visibility, `If-Match`, rules and the conditional write
(ADRs 0001, 0004 and 0009)."""

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orders_api.application.orders import (
    OrderNotFound,
    VersionMismatch,
    change_order_status,
    check_may_change_status,
)
from orders_api.domain.access import Role
from orders_api.domain.order import (
    NEXT_STATUSES,
    Order,
    OrderStatus,
    StatusChangeNotAllowed,
    format_timestamp,
)
from orders_api.domain.permissions import Caller, PermissionDenied
from tests.builders import an_order
from tests.unit.conftest import NOW
from tests.unit.fakes import FixedClock, InMemoryOrderRepository

ALICE = Caller(subject="alice-sub", roles=frozenset({Role.USER}))
BOB = Caller(subject="bob-sub", roles=frozenset({Role.USER}))
MANAGER = Caller(subject="manager-sub", roles=frozenset({Role.ORDER_MANAGER}))
ADMIN = Caller(subject="admin-sub", roles=frozenset({Role.ADMIN}))
NOBODY = Caller(subject="nobody-sub", roles=frozenset())
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"
CLOCK = FixedClock(NOW)


def alices_order(status: OrderStatus = OrderStatus.PENDING, version: int = 1) -> Order:
    return an_order(owner_id="alice-sub", status=status, version=version)


def change(
    repository: InMemoryOrderRepository,
    caller: Caller,
    status: OrderStatus,
    *,
    order_id: str | None = None,
    if_match: frozenset[int] | None = None,
) -> Order:
    return change_order_status(
        repository, CLOCK, caller, order_id or alices_order().id, status, if_match=if_match
    )


# The permission step: decided by the requested status alone, before any data is read.


@pytest.mark.parametrize("status", list(OrderStatus))
def test_nobody_may_request_any_status(status: OrderStatus) -> None:
    with pytest.raises(PermissionDenied):
        check_may_change_status(NOBODY, status)


@pytest.mark.parametrize("status", [s for s in OrderStatus if s is not OrderStatus.CANCELLED])
def test_a_user_may_request_no_status_but_cancelled(status: OrderStatus) -> None:
    check_may_change_status(ALICE, OrderStatus.CANCELLED)
    with pytest.raises(PermissionDenied) as denied:
        check_may_change_status(ALICE, status)

    assert str(denied.value) == "Needs one of: order:status:any"


@pytest.mark.parametrize("staff", [MANAGER, ADMIN], ids=["manager", "admin"])
@pytest.mark.parametrize("status", list(OrderStatus))
def test_staff_may_request_every_status(staff: Caller, status: OrderStatus) -> None:
    check_may_change_status(staff, status)


# Allowed changes, and who may make them.


def test_staff_move_an_order_along_the_lifecycle_one_version_at_a_time() -> None:
    repository = InMemoryOrderRepository([alices_order()])

    for version, status in enumerate(
        [OrderStatus.CONFIRMED, OrderStatus.SHIPPED, OrderStatus.DELIVERED], start=2
    ):
        changed = change(repository, MANAGER, status)

        assert (changed.status, changed.version) == (status, version)
        assert changed.updated_at == format_timestamp(NOW)
        assert repository.get(changed.id) == changed


@pytest.mark.parametrize("current", [OrderStatus.PENDING, OrderStatus.CONFIRMED])
def test_the_owner_cancels_their_own_order_before_it_ships(current: OrderStatus) -> None:
    repository = InMemoryOrderRepository([alices_order(current, version=2)])

    cancelled = change(repository, ALICE, OrderStatus.CANCELLED)

    assert (cancelled.status, cancelled.version) == (OrderStatus.CANCELLED, 3)
    assert repository.get(cancelled.id) == cancelled


@pytest.mark.parametrize("staff", [MANAGER, ADMIN], ids=["manager", "admin"])
def test_staff_cancel_anyone_s_order(staff: Caller) -> None:
    repository = InMemoryOrderRepository([alices_order()])

    assert change(repository, staff, OrderStatus.CANCELLED).status is OrderStatus.CANCELLED


def test_the_owner_cancelling_after_shipping_is_not_allowed() -> None:
    order = alices_order(OrderStatus.SHIPPED, version=3)
    repository = InMemoryOrderRepository([order])

    with pytest.raises(StatusChangeNotAllowed):
        change(repository, ALICE, OrderStatus.CANCELLED)

    assert repository.get(order.id) == order


def test_requesting_the_current_status_changes_nothing() -> None:
    order = alices_order(OrderStatus.CONFIRMED, version=2)
    repository = InMemoryOrderRepository([order])

    assert change(repository, MANAGER, OrderStatus.CONFIRMED) == order
    assert repository.get(order.id) == order
    assert repository.updates_attempted == 0


# Visibility: someone else's order is a 404 for a caller who may only act on their own.


def test_an_unknown_order_is_not_found() -> None:
    with pytest.raises(OrderNotFound):
        change(InMemoryOrderRepository(), MANAGER, OrderStatus.CONFIRMED, order_id=UNKNOWN_ID)


def test_bob_cancelling_alice_s_order_is_not_found_like_an_unknown_one() -> None:
    order = alices_order()
    repository = InMemoryOrderRepository([order])

    with pytest.raises(OrderNotFound) as foreign:
        change(repository, BOB, OrderStatus.CANCELLED)

    assert str(foreign.value) == str(OrderNotFound(order.id))
    assert repository.get(order.id) == order


def test_a_hidden_order_is_not_found_even_when_the_change_would_be_refused() -> None:
    repository = InMemoryOrderRepository([alices_order(OrderStatus.DELIVERED, version=4)])

    with pytest.raises(OrderNotFound):
        change(repository, BOB, OrderStatus.CANCELLED, if_match=frozenset({1}))


# If-Match: a mismatch is a 412, and it is decided before the rules (409) or a no-op (200).


def test_a_matching_if_match_lets_the_change_through() -> None:
    repository = InMemoryOrderRepository([alices_order(version=1)])

    changed = change(repository, MANAGER, OrderStatus.CONFIRMED, if_match=frozenset({1}))

    assert changed.version == 2


def test_if_match_may_list_several_versions() -> None:
    repository = InMemoryOrderRepository([alices_order(version=2)])

    assert (
        change(repository, MANAGER, OrderStatus.CONFIRMED, if_match=frozenset({1, 2})).version == 3
    )


@pytest.mark.parametrize(
    "requested",
    [OrderStatus.CONFIRMED, OrderStatus.PENDING, OrderStatus.DELIVERED],
    ids=["allowed", "same-status", "not-allowed"],
)
def test_a_stale_if_match_is_a_version_mismatch_whatever_was_requested(
    requested: OrderStatus,
) -> None:
    order = alices_order(OrderStatus.PENDING, version=2)
    repository = InMemoryOrderRepository([order])

    with pytest.raises(VersionMismatch) as mismatch:
        change(repository, MANAGER, requested, if_match=frozenset({1}))

    assert mismatch.value.current_version == 2
    assert repository.get(order.id) == order


def test_no_version_matches_an_empty_if_match() -> None:
    repository = InMemoryOrderRepository([alices_order()])

    with pytest.raises(VersionMismatch):
        change(repository, MANAGER, OrderStatus.CONFIRMED, if_match=frozenset())


# Races: the conditional write never overwrites; the loser is decided again on the fresh state.


def test_the_owner_cancelling_while_the_manager_ships_loses_the_race_with_a_refusal() -> None:
    repository = InMemoryOrderRepository([alices_order(OrderStatus.CONFIRMED, version=2)])
    repository.interleave(lambda: change(repository, MANAGER, OrderStatus.SHIPPED))

    with pytest.raises(StatusChangeNotAllowed) as refused:
        change(repository, ALICE, OrderStatus.CANCELLED)

    assert refused.value.current is OrderStatus.SHIPPED
    stored = repository.get(alices_order().id)
    assert stored is not None
    assert (stored.status, stored.version) == (OrderStatus.SHIPPED, 3)


def test_the_manager_shipping_while_the_owner_cancels_loses_the_race_with_a_refusal() -> None:
    repository = InMemoryOrderRepository([alices_order(OrderStatus.CONFIRMED, version=2)])
    repository.interleave(lambda: change(repository, ALICE, OrderStatus.CANCELLED))

    with pytest.raises(StatusChangeNotAllowed):
        change(repository, MANAGER, OrderStatus.SHIPPED)

    stored = repository.get(alices_order().id)
    assert stored is not None
    assert (stored.status, stored.version) == (OrderStatus.CANCELLED, 3)


def test_losing_to_the_same_change_is_a_no_op_with_the_winner_s_version() -> None:
    repository = InMemoryOrderRepository([alices_order()])
    repository.interleave(lambda: change(repository, ADMIN, OrderStatus.CONFIRMED))

    result = change(repository, MANAGER, OrderStatus.CONFIRMED)

    assert (result.status, result.version) == (OrderStatus.CONFIRMED, 2)
    assert repository.get(result.id) == result


def test_a_change_still_allowed_after_losing_a_race_is_applied_on_top() -> None:
    repository = InMemoryOrderRepository([alices_order()])
    repository.interleave(lambda: change(repository, MANAGER, OrderStatus.CONFIRMED))

    cancelled = change(repository, ALICE, OrderStatus.CANCELLED)

    assert (cancelled.status, cancelled.version) == (OrderStatus.CANCELLED, 3)


def test_losing_a_race_with_if_match_is_a_version_mismatch() -> None:
    repository = InMemoryOrderRepository([alices_order()])
    repository.interleave(lambda: change(repository, MANAGER, OrderStatus.CONFIRMED))

    with pytest.raises(VersionMismatch) as mismatch:
        change(repository, ALICE, OrderStatus.CANCELLED, if_match=frozenset({1}))

    assert mismatch.value.current_version == 2


def test_an_order_deleted_during_the_change_is_not_found() -> None:
    repository = InMemoryOrderRepository([alices_order()])
    repository.interleave(lambda: repository.delete(alices_order().id))

    with pytest.raises(OrderNotFound):
        change(repository, MANAGER, OrderStatus.CONFIRMED)

    assert repository.get(alices_order().id) is None


class RefusingRepository(InMemoryOrderRepository):
    """A store whose conditional write always fails although nothing changed: a broken adapter."""

    def update_status(self, current: Order, changed: Order, *, check_version: bool) -> None:
        self.updates_attempted += 1


def test_a_write_that_never_succeeds_gives_up_instead_of_looping() -> None:
    repository = RefusingRepository([alices_order()])

    with pytest.raises(RuntimeError):
        change(repository, MANAGER, OrderStatus.CONFIRMED)

    assert repository.updates_attempted == len(OrderStatus)


# A lost race is decided as if the loser had come second (ADR 0004).

STAFF_OR_OWNER = st.sampled_from([ALICE, MANAGER])


def outcome(repository: InMemoryOrderRepository, caller: Caller, status: OrderStatus) -> object:
    try:
        return change(repository, caller, status)
    except (StatusChangeNotAllowed, PermissionDenied) as error:
        return type(error)


def request(repository: InMemoryOrderRepository, caller: Caller, status: OrderStatus) -> object:
    """The permission step, then the use case, as the API function runs them."""
    try:
        check_may_change_status(caller, status)
    except PermissionDenied:
        return PermissionDenied
    return outcome(repository, caller, status)


@given(
    st.sampled_from(OrderStatus),
    STAFF_OR_OWNER,
    st.sampled_from(OrderStatus),
    STAFF_OR_OWNER,
    st.sampled_from(OrderStatus),
)
def test_the_loser_of_a_race_gets_what_it_would_get_after_the_winner(
    start: OrderStatus,
    loser: Caller,
    loser_wants: OrderStatus,
    winner: Caller,
    winner_wants: OrderStatus,
) -> None:
    racing = InMemoryOrderRepository([alices_order(start)])
    racing.interleave(lambda: request(racing, winner, winner_wants))
    raced = request(racing, loser, loser_wants)

    sequential = InMemoryOrderRepository([alices_order(start)])
    if racing.interleaved:
        request(sequential, winner, winner_wants)
    in_turn = request(sequential, loser, loser_wants)

    assert raced == in_turn
    assert racing.get(alices_order().id) == sequential.get(alices_order().id)


@given(
    st.lists(
        st.tuples(st.sampled_from([ALICE, BOB, MANAGER, NOBODY]), st.sampled_from(OrderStatus))
    )
)
def test_random_requests_never_skip_a_step_or_leave_a_final_status(
    requests: list[tuple[Caller, OrderStatus]],
) -> None:
    repository = InMemoryOrderRepository([alices_order()])
    previous = alices_order()

    for caller, status in requests:
        try:
            check_may_change_status(caller, status)
            change(repository, caller, status)
        except (PermissionDenied, OrderNotFound, StatusChangeNotAllowed):
            pass
        stored = repository.get(previous.id)
        assert stored is not None
        if stored != previous:
            assert stored.status in NEXT_STATUSES[previous.status]
            assert stored == replace(
                previous,
                status=stored.status,
                updated_at=stored.updated_at,
                version=previous.version + 1,
            )
            assert caller in (MANAGER, ALICE)
            assert caller is MANAGER or stored.status is OrderStatus.CANCELLED
        previous = stored
