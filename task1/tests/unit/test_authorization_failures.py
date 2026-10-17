"""Authorization in the request pipeline: `400` before `403` before `404` (ADRs 0001 and 0009)."""

import io
import json

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.domain.order import OrderStatus
from tests.builders import an_order
from tests.unit.events import (
    ADMIN,
    ALICE,
    BOB,
    MANAGER,
    NOBODY,
    VALID_ORDER,
    change_status_event,
    create_order_event,
    delete_order_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import InMemoryOrderRepository, WireApi

ALICES_ORDER = an_order(owner_id="alice-sub")
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"
NEW_ID = "0e6bb2c6-0000-4000-8000-0000000000aa"


class UntouchableRepository(InMemoryOrderRepository):
    """A `403` is decided before any data is read or written (ADR 0001)."""

    def get(self, order_id: str) -> None:
        raise AssertionError("the repository must not be read")

    def add(self, order: object) -> None:
        raise AssertionError("the repository must not be written")

    def update_status(self, current: object, changed: object, *, check_version: bool) -> None:
        raise AssertionError("the repository must not be written")

    def delete(self, order_id: str) -> bool:
        raise AssertionError("the repository must not be written")


@pytest.fixture(autouse=True)
def repository(wire_api: WireApi) -> InMemoryOrderRepository:
    return wire_api([ALICES_ORDER], ids=[NEW_ID])


@pytest.fixture
def untouchable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_handler, "_orders", UntouchableRepository())


def with_roles(roles: str) -> dict[str, str]:
    return {"principalId": "multi-sub", "sub": "multi-sub", "roles": roles}


# No recognised role: 403 on every order endpoint, before any data access.


@pytest.mark.usefixtures("untouchable")
def test_nobody_may_not_create_an_order() -> None:
    body = problem_of(invoke(create_order_event(authorizer=NOBODY)), 403, "forbidden")

    assert body["instance"] == "/api/v1/orders"
    assert body["detail"] == "Missing permission: order:create"


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("order_id", [ALICES_ORDER.id, UNKNOWN_ID], ids=["existing", "unknown"])
def test_nobody_may_not_read_an_order_whether_or_not_it_exists(order_id: str) -> None:
    body = problem_of(invoke(get_order_event(order_id, NOBODY)), 403, "forbidden")

    assert body["detail"] == "Missing permission: order:read:any or order:read:own"


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("status", [status.value for status in OrderStatus])
@pytest.mark.parametrize("order_id", [ALICES_ORDER.id, UNKNOWN_ID], ids=["existing", "unknown"])
def test_nobody_may_not_change_any_status_whether_or_not_the_order_exists(
    order_id: str, status: str
) -> None:
    body = problem_of(
        invoke(change_status_event(order_id, status=status, authorizer=NOBODY)), 403, "forbidden"
    )

    assert body["instance"] == f"/api/v1/orders/{order_id}/status"


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("order_id", [ALICES_ORDER.id, UNKNOWN_ID], ids=["existing", "unknown"])
def test_nobody_may_not_delete_an_order_whether_or_not_it_exists(order_id: str) -> None:
    body = problem_of(invoke(delete_order_event(order_id, NOBODY)), 403, "forbidden")

    assert body["detail"] == "Missing permission: order:delete:any"


@pytest.mark.usefixtures("untouchable")
def test_only_unknown_groups_are_no_role_at_all() -> None:
    problem_of(
        invoke(get_order_event(ALICES_ORDER.id, with_roles("marketing,staff"))), 403, "forbidden"
    )


# The check order: 400 before 403 before 404.


@pytest.mark.usefixtures("untouchable")
def test_an_invalid_body_is_a_400_even_for_nobody() -> None:
    event = create_order_event({**VALID_ORDER, "currency": "XXX"}, authorizer=NOBODY)

    problem_of(invoke(event), 400, "validation-error")


@pytest.mark.usefixtures("untouchable")
def test_a_malformed_path_id_is_a_400_even_for_nobody() -> None:
    problem_of(invoke(get_order_event("not-a-uuid", NOBODY)), 400, "validation-error")


@pytest.mark.usefixtures("untouchable")
def test_an_invalid_status_body_is_a_400_even_for_nobody() -> None:
    event = change_status_event(ALICES_ORDER.id, status="LOST", authorizer=NOBODY)

    problem_of(invoke(event), 400, "validation-error")


@pytest.mark.usefixtures("untouchable")
def test_a_malformed_path_id_is_a_400_for_nobody_on_status_changes_and_deletes() -> None:
    problem_of(invoke(change_status_event("not-a-uuid", authorizer=NOBODY)), 400)
    problem_of(invoke(delete_order_event("not-a-uuid", NOBODY)), 400)


# Status changes and deletion: who may, decided before any data is read.


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("status", ["PENDING", "CONFIRMED", "SHIPPED", "DELIVERED"])
def test_the_owner_may_not_request_any_status_but_cancelled_even_on_their_own_order(
    status: str,
) -> None:
    body = problem_of(
        invoke(change_status_event(ALICES_ORDER.id, status=status, authorizer=ALICE)),
        403,
        "forbidden",
    )

    assert body["detail"] == "Missing permission: order:status:any"


@pytest.mark.usefixtures("untouchable")
@pytest.mark.parametrize("caller", [ALICE, MANAGER], ids=["user", "manager"])
def test_only_an_admin_may_delete(caller: dict[str, str]) -> None:
    body = problem_of(invoke(delete_order_event(ALICES_ORDER.id, caller)), 403, "forbidden")

    assert body["detail"] == "Missing permission: order:delete:any"


def test_bob_cancelling_alice_s_order_is_a_404_identical_to_an_unknown_one() -> None:
    foreign = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=BOB))
    unknown = invoke(change_status_event(UNKNOWN_ID, status="CANCELLED", authorizer=BOB))

    foreign_body = problem_of(foreign, 404, "not-found")
    unknown_body = problem_of(unknown, 404, "not-found")
    assert {k: v for k, v in foreign_body.items() if k != "instance"} == {
        k: v for k, v in unknown_body.items() if k != "instance"
    }


def test_the_owner_cancels_their_own_order() -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=ALICE))

    assert response["statusCode"] == 200


@pytest.mark.parametrize("staff", [MANAGER, ADMIN], ids=["manager", "admin"])
def test_staff_change_the_status_of_anyone_s_order(staff: dict[str, str]) -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, status="CONFIRMED", authorizer=staff))

    assert response["statusCode"] == 200


def test_someone_else_s_order_is_a_404_identical_to_an_unknown_one() -> None:
    foreign = invoke(get_order_event(ALICES_ORDER.id, BOB))
    unknown = invoke(get_order_event(UNKNOWN_ID, BOB))

    foreign_body = problem_of(foreign, 404, "not-found")
    unknown_body = problem_of(unknown, 404, "not-found")
    # Only the echoed path differs; nothing tells that the order exists.
    assert {k: v for k, v in foreign_body.items() if k != "instance"} == {
        k: v for k, v in unknown_body.items() if k != "instance"
    }
    assert set(headers_of(foreign)) == set(headers_of(unknown))


# Who reads what.


def test_the_owner_reads_their_own_order() -> None:
    assert invoke(get_order_event(ALICES_ORDER.id, ALICE))["statusCode"] == 200


@pytest.mark.parametrize("staff", [MANAGER, ADMIN], ids=["manager", "admin"])
def test_staff_read_anyone_s_order(staff: dict[str, str]) -> None:
    response = invoke(get_order_event(ALICES_ORDER.id, staff))

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["ownerId"] == "alice-sub"


def test_several_groups_get_the_union_and_unknown_ones_are_ignored() -> None:
    caller = with_roles("marketing,USER,ORDER_MANAGER")

    assert invoke(get_order_event(ALICES_ORDER.id, caller))["statusCode"] == 200
    assert invoke(create_order_event(authorizer=caller))["statusCode"] == 201


@pytest.mark.parametrize("caller", [ALICE, MANAGER, ADMIN], ids=["user", "manager", "admin"])
def test_every_role_creates_orders_for_itself(caller: dict[str, str]) -> None:
    response = invoke(create_order_event(authorizer=caller))

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["ownerId"] == caller["sub"]


def test_the_access_line_records_the_denial(log_output: io.StringIO) -> None:
    invoke(get_order_event(ALICES_ORDER.id, NOBODY))

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    access = next(line for line in lines if line["message"] == "request")
    assert access["status"] == 403
    assert access["problemType"] == "urn:problem:orders-api:forbidden"
    assert access["sub"] == "nobody-sub"
    assert access["roles"] == []
