"""Listing orders: the query parameters and the cursor (ADR 0004; pipeline step 4, ADR 0009).

The cursor is opaque to clients: unpadded base64url over a small JSON object holding the list it
belongs to (`owner`, `null` for every owner's orders) and the position the next page starts
after (the last order's `createdAt` and `id`, from which the store rebuilds its own key). A
cursor that doesn't decode is rejected with the other parameter checks, before the permission
check; whether it belongs to the caller's list can only be told once their permissions are
known, so that is checked afterwards, still before any data is read.
"""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from functools import cache
from typing import Any

from aws_lambda_powertools.utilities.data_classes.common import BaseProxyEvent

from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.adapters.inbound.order_requests import ORDERS_PATH
from orders_api.adapters.inbound.problems import FieldError, ProblemType
from orders_api.adapters.inbound.request_checks import InvalidRequest
from orders_api.application.orders import OrderPage, PagePosition
from orders_api.contract import property_schema, query_parameter_schemas

LIMIT = "limit"
CURSOR = "cursor"
_CURSOR_FIELDS = {"owner", "createdAt", "id"}
_NOT_A_CURSOR = FieldError(parameter=CURSOR, detail="is not a valid cursor")


@dataclass(frozen=True)
class Cursor:
    owner_id: str | None
    """The list the cursor belongs to: one owner's orders, or every order (`None`)."""
    position: PagePosition


@dataclass(frozen=True)
class OrderListQuery:
    limit: int
    cursor: Cursor | None

    def start_in(self, owner_id: str | None) -> PagePosition | None:
        """Where the caller's list resumes; a cursor issued for another list is a `400`."""
        if self.cursor is None:
            return None
        if self.cursor.owner_id != owner_id:
            raise _invalid(
                FieldError(parameter=CURSOR, detail="belongs to a different list of orders")
            )
        return self.cursor.position


def _invalid(*errors: FieldError) -> InvalidRequest:
    names = ", ".join(str(error.parameter) for error in errors)
    return InvalidRequest(
        ProblemType.VALIDATION_ERROR,
        f"Invalid query parameter{'s' if len(errors) > 1 else ''}: {names}",
        errors,
    )


def encode_cursor(cursor: Cursor) -> str:
    document = {
        "owner": cursor.owner_id,
        "createdAt": cursor.position.created_at,
        "id": cursor.position.order_id,
    }
    raw = json.dumps(document, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@cache
def _order_field_pattern(name: str) -> re.Pattern[str]:
    """The pattern the contract declares for an `Order` field, so a cursor carries only values
    an order could have."""
    return re.compile(property_schema("Order", name)["pattern"])


def _matches(value: Any, field: str) -> bool:
    return isinstance(value, str) and _order_field_pattern(field).fullmatch(value) is not None


def _is_cursor(document: Any) -> bool:
    return (
        isinstance(document, dict)
        and document.keys() == _CURSOR_FIELDS
        and (document["owner"] is None or isinstance(document["owner"], str))
        and _matches(document["createdAt"], "createdAt")
        and _matches(document["id"], "id")
    )


def decode_cursor(text: str) -> Cursor:
    """The cursor as issued by `encode_cursor`; anything else is a `400`."""
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        document = json.loads(raw)
    except (binascii.Error, ValueError):  # ValueError includes JSON and UTF-8 errors
        raise _invalid(_NOT_A_CURSOR) from None
    if not _is_cursor(document):
        raise _invalid(_NOT_A_CURSOR)
    position = PagePosition(created_at=document["createdAt"], order_id=document["id"])
    return Cursor(owner_id=document["owner"], position=position)


def _limit(text: str, schema: dict[str, Any]) -> int:
    """Digits only, within the range the contract declares."""
    minimum, maximum = schema["minimum"], schema["maximum"]
    try:
        value = int(text) if re.fullmatch("[0-9]+", text) else None
    except ValueError:  # more digits than Python converts
        value = None
    if value is None or not minimum <= value <= maximum:
        raise _invalid(
            FieldError(parameter=LIMIT, detail=f"must be an integer from {minimum} to {maximum}")
        )
    return value


def _query_parameters(event: BaseProxyEvent) -> dict[str, list[str]]:
    """Every query parameter with all its values; REST APIs list repeated ones in the multi-value
    map."""
    multi_value = event.multi_value_query_string_parameters
    if multi_value:
        return {name: list(values) for name, values in multi_value.items()}
    return {name: [value] for name, value in event.query_string_parameters.items()}


def order_list_query(event: BaseProxyEvent) -> OrderListQuery:
    """`limit` and `cursor`, each at most once, and nothing else (ADR 0004). Every offending
    parameter is reported in one `400`."""
    declared = query_parameter_schemas("get", ORDERS_PATH)
    limit: int = declared[LIMIT]["default"]
    cursor = None
    errors: list[FieldError] = []
    for name, values in sorted(_query_parameters(event).items()):
        try:
            if name not in declared:
                raise _invalid(
                    FieldError(parameter=name, detail="is not a parameter of this operation")
                )
            if len(values) > 1:
                raise _invalid(FieldError(parameter=name, detail="must be given only once"))
            if name == LIMIT:
                limit = _limit(values[0], declared[LIMIT])
            else:
                cursor = decode_cursor(values[0])
        except InvalidRequest as invalid:
            errors.extend(invalid.errors)
    if errors:
        raise _invalid(*errors)
    return OrderListQuery(limit=limit, cursor=cursor)


def order_page_to_json(page: OrderPage, owner_id: str | None) -> dict[str, Any]:
    """The published `OrderPage`; its cursor is bound to the list it came from."""
    next_cursor = (
        None
        if page.next_position is None
        else encode_cursor(Cursor(owner_id=owner_id, position=page.next_position))
    )
    return {"items": [order_to_json(order) for order in page.orders], "nextCursor": next_cursor}
