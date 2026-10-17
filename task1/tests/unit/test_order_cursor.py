"""The list cursor: an opaque, base64url-encoded position bound to its list (ADR 0004)."""

import base64
import json
import re

import pytest

from orders_api.adapters.inbound.order_list import (
    Cursor,
    OrderListQuery,
    decode_cursor,
    encode_cursor,
)
from orders_api.adapters.inbound.request_checks import InvalidRequest
from orders_api.application.orders import PagePosition

POSITION = PagePosition(
    created_at="2026-10-17T09:30:00.123Z", order_id="3f0c2f40-9a5e-4b1a-9a43-1d6c2a7b8e10"
)
ALICES = Cursor(owner_id="alice-sub", position=POSITION)
EVERYONES = Cursor(owner_id=None, position=POSITION)


def base64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def rejected(text: str) -> InvalidRequest:
    with pytest.raises(InvalidRequest) as invalid:
        decode_cursor(text)
    return invalid.value


@pytest.mark.parametrize("cursor", [ALICES, EVERYONES], ids=["one-owner", "every-owner"])
def test_a_cursor_decodes_to_what_was_encoded(cursor: Cursor) -> None:
    assert decode_cursor(encode_cursor(cursor)) == cursor


def test_a_cursor_is_unpadded_base64url_so_it_needs_no_escaping_in_a_query() -> None:
    owner_with_awkward_bytes = Cursor(owner_id="ä?/+>" * 7, position=POSITION)

    assert re.fullmatch(r"[A-Za-z0-9_-]+", encode_cursor(owner_with_awkward_bytes))


def test_a_cursor_carries_the_position_and_the_list_it_belongs_to() -> None:
    padded = encode_cursor(ALICES) + "=" * (-len(encode_cursor(ALICES)) % 4)

    assert json.loads(base64.urlsafe_b64decode(padded)) == {
        "owner": "alice-sub",
        "createdAt": POSITION.created_at,
        "id": POSITION.order_id,
    }


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not base64!",
        "eyJvd25lciI6",  # base64url, but cut off in the middle of the JSON
        base64url("not json"),
        base64url("[]"),
        base64url('{"owner": "alice-sub", "createdAt": "2026-10-17T09:30:00.123Z"}'),
        base64url(
            json.dumps({"owner": 7, "createdAt": POSITION.created_at, "id": POSITION.order_id})
        ),
        base64url(json.dumps({"owner": None, "createdAt": "yesterday", "id": POSITION.order_id})),
        base64url(json.dumps({"owner": None, "createdAt": POSITION.created_at, "id": "x#y"})),
        base64url(
            json.dumps(
                {"owner": None, "createdAt": POSITION.created_at, "id": POSITION.order_id, "x": 1}
            )
        ),
    ],
    ids=[
        "empty",
        "not-base64",
        "truncated",
        "not-json",
        "not-an-object",
        "missing-id",
        "owner-not-a-string",
        "created-at-not-a-timestamp",
        "id-not-a-uuid",
        "unknown-field",
    ],
)
def test_anything_that_is_not_an_issued_cursor_is_a_400_naming_the_parameter(text: str) -> None:
    error = rejected(text)

    assert error.problem.status == 400
    assert error.detail == "Invalid query parameter: cursor"
    assert [(e.parameter, e.detail) for e in error.errors] == [("cursor", "is not a valid cursor")]


# Binding to the list: a cursor only resumes the list it was issued for.


def test_a_cursor_resumes_the_list_it_was_issued_for() -> None:
    assert OrderListQuery(limit=10, cursor=ALICES).start_in("alice-sub") == POSITION
    assert OrderListQuery(limit=10, cursor=EVERYONES).start_in(None) == POSITION


def test_without_a_cursor_a_list_starts_at_the_newest_order() -> None:
    assert OrderListQuery(limit=10, cursor=None).start_in("alice-sub") is None


@pytest.mark.parametrize(
    ("cursor", "listed_owner"),
    [(ALICES, "bob-sub"), (ALICES, None), (EVERYONES, "alice-sub")],
    ids=["another-owner-s", "one-owner-s-in-every-owner-s", "every-owner-s-in-one-owner-s"],
)
def test_a_cursor_from_another_list_is_a_400(cursor: Cursor, listed_owner: str | None) -> None:
    with pytest.raises(InvalidRequest) as invalid:
        OrderListQuery(limit=10, cursor=cursor).start_in(listed_owner)

    assert invalid.value.problem.status == 400
    assert invalid.value.detail == "Invalid query parameter: cursor"
    assert [(e.parameter, e.detail) for e in invalid.value.errors] == [
        ("cursor", "belongs to a different list of orders")
    ]
