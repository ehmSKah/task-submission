"""`PATCH /api/v1/orders/{id}/status` through the API function (ADRs 0004, 0007 and 0009)."""

import io
import json
from typing import Any

import pytest

from orders_api.adapters.inbound import api_handler
from orders_api.adapters.inbound.request_checks import MAX_BODY_BYTES
from orders_api.domain.order import OrderStatus
from tests.builders import an_order
from tests.unit.events import (
    ADMIN,
    ALICE,
    BOB,
    change_status_event,
    get_order_event,
    headers_of,
    invoke,
    problem_of,
)
from tests.unit.fakes import InMemoryOrderRepository, WireApi

ALICES_ORDER = an_order(owner_id="alice-sub")
UNKNOWN_ID = "0e6bb2c6-0000-4000-8000-000000000000"


class UntouchableRepository(InMemoryOrderRepository):
    """Request checks happen before any data access (ADR 0009)."""

    def get(self, order_id: str) -> None:
        raise AssertionError("the repository must not be read")


@pytest.fixture(autouse=True)
def repository(wire_api: WireApi) -> InMemoryOrderRepository:
    return wire_api([ALICES_ORDER])


def stored_status(repository: InMemoryOrderRepository) -> tuple[OrderStatus, int]:
    stored = repository.get(ALICES_ORDER.id)
    assert stored is not None
    return stored.status, stored.version


def test_the_manager_confirms_an_order_and_gets_it_back_with_its_new_etag(
    repository: InMemoryOrderRepository,
) -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, status="CONFIRMED"))

    assert response["statusCode"] == 200
    assert headers_of(response)["Content-Type"] == "application/json"
    assert headers_of(response)["ETag"] == '"2"'
    body = json.loads(response["body"])
    assert (body["status"], body["version"]) == ("CONFIRMED", 2)
    assert body["updatedAt"] == "2026-10-17T09:30:00.000Z"
    assert stored_status(repository) == (OrderStatus.CONFIRMED, 2)


def test_the_changed_order_reads_back_with_the_same_body_and_etag() -> None:
    changed = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=ALICE))

    read = invoke(get_order_event(ALICES_ORDER.id, ALICE))

    assert json.loads(read["body"]) == json.loads(changed["body"])
    assert headers_of(read)["ETag"] == headers_of(changed)["ETag"] == '"2"'


def test_the_same_status_again_is_a_200_with_the_version_unchanged() -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, status="PENDING"))

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["version"] == 1
    assert headers_of(response)["ETag"] == '"1"'


def test_a_change_the_lifecycle_doesn_t_allow_is_a_409(wire_api: WireApi) -> None:
    repository = wire_api([an_order(owner_id="alice-sub", status=OrderStatus.SHIPPED)])

    response = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=ALICE))

    body = problem_of(response, 409, "conflict")
    assert body["detail"] == "An order can't change from SHIPPED to CANCELLED"
    assert body["instance"] == f"/api/v1/orders/{ALICES_ORDER.id}/status"
    assert stored_status(repository) == (OrderStatus.SHIPPED, 1)


def test_the_owner_losing_a_race_against_shipping_gets_a_409(wire_api: WireApi) -> None:
    repository = wire_api([an_order(owner_id="alice-sub", status=OrderStatus.CONFIRMED)])
    repository.interleave(lambda: invoke(change_status_event(ALICES_ORDER.id, status="SHIPPED")))

    response = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=ALICE))

    body = problem_of(response, 409, "conflict")
    assert body["detail"] == "An order can't change from SHIPPED to CANCELLED"
    assert stored_status(repository) == (OrderStatus.SHIPPED, 2)


def test_a_change_still_allowed_after_losing_a_race_is_a_200(
    repository: InMemoryOrderRepository,
) -> None:
    repository.interleave(lambda: invoke(change_status_event(ALICES_ORDER.id, authorizer=ADMIN)))

    response = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=ALICE))

    # The admin's confirmation won; cancelling a confirmed order is still allowed.
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["version"] == 3


# If-Match (ADR 0004): optional; the order's ETag, compared strongly.


@pytest.mark.parametrize(
    "if_match",
    ['"1"', "*", '"7", "1"', "", '  "1"  '],
    ids=["its-etag", "any", "a-list", "empty", "padded"],
)
def test_a_matching_or_empty_if_match_lets_the_change_through(if_match: str) -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, if_match=if_match))

    assert response["statusCode"] == 200
    assert headers_of(response)["ETag"] == '"2"'


@pytest.mark.parametrize(
    "if_match",
    ['"2"', 'W/"1"', "1", '"one"', '"' + "1" * 5000 + '"'],
    ids=["stale", "weak", "unquoted", "not-a-version", "huge"],
)
def test_an_if_match_naming_no_current_version_is_a_412(
    repository: InMemoryOrderRepository, if_match: str
) -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, if_match=if_match))

    body = problem_of(response, 412, "precondition-failed")
    assert body["detail"] == "If-Match doesn't name the order's current ETag"
    assert stored_status(repository) == (OrderStatus.PENDING, 1)


def test_the_if_match_header_name_is_matched_in_any_casing() -> None:
    event = change_status_event(ALICES_ORDER.id)
    event["headers"]["if-match"] = event["multiValueHeaders"]["if-match"] = '"9"'

    problem_of(invoke(event), 412, "precondition-failed")


@pytest.mark.parametrize("status", ["PENDING", "DELIVERED"], ids=["same-status", "not-allowed"])
def test_a_stale_if_match_is_a_412_before_a_no_op_or_a_409(status: str) -> None:
    response = invoke(change_status_event(ALICES_ORDER.id, status=status, if_match='"0"'))

    problem_of(response, 412, "precondition-failed")


# Request checks: 400 / 413 / 415 before any data is read.


@pytest.mark.parametrize(
    ("event", "status", "slug"),
    [
        (change_status_event(ALICES_ORDER.id, status="LOST"), 400, "validation-error"),
        (change_status_event(ALICES_ORDER.id, {"status": "pending"}), 400, "validation-error"),
        (change_status_event(ALICES_ORDER.id, {"state": "SHIPPED"}), 400, "validation-error"),
        (
            change_status_event(ALICES_ORDER.id, {"status": "SHIPPED", "version": 1}),
            400,
            "validation-error",
        ),
        (change_status_event(ALICES_ORDER.id, raw='["SHIPPED"]'), 400, "validation-error"),
        (change_status_event(ALICES_ORDER.id, raw="{"), 400, "validation-error"),
        (change_status_event(ALICES_ORDER.id, raw=""), 400, "validation-error"),
        (
            change_status_event(ALICES_ORDER.id, content_type="text/plain"),
            415,
            "unsupported-media-type",
        ),
        (change_status_event(ALICES_ORDER.id, content_type=None), 415, "unsupported-media-type"),
        (
            change_status_event(ALICES_ORDER.id, raw=" " * (MAX_BODY_BYTES + 1)),
            413,
            "payload-too-large",
        ),
        (change_status_event("not-a-uuid"), 400, "validation-error"),
    ],
    ids=[
        "unknown-status",
        "lower-case-status",
        "missing-status",
        "unknown-field",
        "not-an-object",
        "invalid-json",
        "empty",
        "content-type",
        "no-content-type",
        "oversized",
        "malformed-id",
    ],
)
def test_invalid_requests_are_problems_without_data_access(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], status: int, slug: str
) -> None:
    monkeypatch.setattr(api_handler, "_orders", UntouchableRepository())

    problem_of(invoke(event), status, slug)


def test_an_unknown_status_names_the_field() -> None:
    body = problem_of(invoke(change_status_event(ALICES_ORDER.id, status="LOST")), 400)

    assert [error["pointer"] for error in body["errors"]] == ["/status"]
    assert body["detail"].startswith("Invalid request body: $.status:")


def test_a_malformed_id_names_the_parameter() -> None:
    body = problem_of(invoke(change_status_event("not-a-uuid")), 400)

    assert body["errors"] == [
        {"parameter": "id", "detail": body["errors"][0]["detail"]},
    ]


# Visibility: 404 for an unknown order, and for someone else's to a caller who may only cancel.


def test_bob_cancelling_alice_s_order_gets_the_same_404_as_for_an_unknown_one() -> None:
    foreign = invoke(change_status_event(ALICES_ORDER.id, status="CANCELLED", authorizer=BOB))
    unknown = invoke(change_status_event(UNKNOWN_ID, status="CANCELLED", authorizer=BOB))

    foreign_body = problem_of(foreign, 404, "not-found")
    unknown_body = problem_of(unknown, 404, "not-found")
    assert {k: v for k, v in foreign_body.items() if k != "instance"} == {
        k: v for k, v in unknown_body.items() if k != "instance"
    }


def test_missing_authorizer_context_fails_closed() -> None:
    problem_of(invoke(change_status_event(ALICES_ORDER.id, authorizer=None)), 500, "internal-error")


def test_the_access_line_names_the_changed_order(log_output: io.StringIO) -> None:
    invoke(change_status_event(ALICES_ORDER.id))

    lines = [json.loads(line) for line in log_output.getvalue().splitlines() if line]
    access = next(line for line in lines if line["message"] == "request")
    assert access["status"] == 200
    assert access["route"] == "PATCH /api/v1/orders/{id}/status"
    assert access["orderId"] == ALICES_ORDER.id
    assert access["sub"] == "manager-sub"
