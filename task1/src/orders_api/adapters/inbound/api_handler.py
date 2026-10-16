"""Lambda entry point for the API function: routing and the HTTP response contract (ADR 0009)."""

import json
import os
import time
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.utilities.typing import LambdaContext

from orders_api.adapters.inbound.caller import Caller, MissingCallerIdentity, caller_from_event
from orders_api.adapters.inbound.http_headers import (
    JSON_MEDIA_TYPE,
    PROBLEM_JSON_MEDIA_TYPE,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
)
from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.adapters.inbound.order_requests import new_order_from, valid_order_id
from orders_api.adapters.inbound.problems import FieldError, ProblemType, problem_body
from orders_api.adapters.inbound.request_checks import InvalidRequest
from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from orders_api.adapters.outbound.system import RandomOrderIds, SystemClock
from orders_api.application.orders import (
    Clock,
    IdGenerator,
    OrderNotFound,
    OrderRepository,
    create_order,
    get_order,
)
from orders_api.domain.order import Order

UNEXPECTED_ERROR_DETAIL = "An unexpected error occurred"

logger = Logger(service="orders-api")
app = APIGatewayRestResolver()

_orders: OrderRepository | None = None
clock: Clock = SystemClock()
order_ids: IdGenerator = RandomOrderIds()


def orders() -> OrderRepository:
    """The order repository, created once per Lambda instance."""
    global _orders
    if _orders is None:
        _orders = DynamoDbOrderRepository(os.environ["ORDERS_TABLE"])
    return _orders


def health_status() -> dict[str, str]:
    """Shallow liveness: the function runs. No dependency is called (ADR 0002)."""
    return {"status": "ok"}


@app.get("/api/v1/health")
def get_health() -> Response[str]:
    return Response(status_code=200, content_type=JSON_MEDIA_TYPE, body=json.dumps(health_status()))


def identified_caller() -> Caller:
    """Step 2 of the request pipeline: the authorizer's identity, or fail closed (ADR 0009)."""
    caller = caller_from_event(app.current_event.raw_event)
    logger.append_keys(sub=caller.subject, roles=sorted(caller.roles))
    return caller


def order_response(order: Order, status_code: int) -> Response[str]:
    """An order in its published shape, with its version as the `ETag` (ADR 0004)."""
    logger.append_keys(orderId=order.id)
    return Response(
        status_code=status_code,
        content_type=JSON_MEDIA_TYPE,
        body=json.dumps(order_to_json(order)),
        headers={"ETag": f'"{order.version}"'},
    )


@app.post("/api/v1/orders")
def post_order() -> Response[str]:
    caller = identified_caller()
    request = new_order_from(app.current_event)
    order = create_order(orders(), clock, order_ids, owner_id=caller.subject, request=request)
    return order_response(order, 201)


@app.get("/api/v1/orders/<order_id>")
def get_order_by_id(order_id: str) -> Response[str]:
    identified_caller()
    return order_response(get_order(orders(), valid_order_id(order_id)), 200)


def problem_response(
    problem: ProblemType, detail: str | None = None, errors: tuple[FieldError, ...] = ()
) -> Response[str]:
    body = problem_body(
        problem,
        detail=detail,
        instance=app.current_event.path,
        request_id=app.current_event.request_context.request_id,
        errors=errors,
    )
    return Response(
        status_code=problem.status, content_type=PROBLEM_JSON_MEDIA_TYPE, body=json.dumps(body)
    )


@app.not_found
def not_found(_: Exception) -> Response[str]:
    return problem_response(ProblemType.NOT_FOUND)


@app.exception_handler(InvalidRequest)  # type: ignore[untyped-decorator]
def invalid_request(error: InvalidRequest) -> Response[str]:
    return problem_response(error.problem, error.detail, error.errors)


@app.exception_handler(OrderNotFound)  # type: ignore[untyped-decorator]  # Powertools leaves it untyped
def order_not_found(_: OrderNotFound) -> Response[str]:
    return problem_response(ProblemType.NOT_FOUND)


@app.exception_handler(MissingCallerIdentity)  # type: ignore[untyped-decorator]
def missing_caller_identity(_: MissingCallerIdentity) -> Response[str]:
    logger.error("authorizer_context_missing")
    return problem_response(ProblemType.INTERNAL_ERROR, UNEXPECTED_ERROR_DETAIL)


@app.exception_handler(Exception)  # type: ignore[untyped-decorator]  # Powertools leaves it untyped
def unexpected_error(error: Exception) -> Response[str]:
    logger.exception("unhandled_error", extra={"errorType": type(error).__name__})
    return problem_response(ProblemType.INTERNAL_ERROR, UNEXPECTED_ERROR_DETAIL)


def _unexpected_error_response(path: str, request_id: str) -> dict[str, Any]:
    """Last-resort 500 for failures outside routing, so they never surface as a gateway 502."""
    problem = ProblemType.INTERNAL_ERROR
    body = problem_body(
        problem, detail=UNEXPECTED_ERROR_DETAIL, instance=path, request_id=request_id
    )
    return {
        "statusCode": problem.status,
        "headers": {"Content-Type": PROBLEM_JSON_MEDIA_TYPE},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def _problem_type(response: dict[str, Any]) -> str | None:
    if response.get("statusCode", 200) < 400:
        return None
    try:
        problem_type: str | None = json.loads(response.get("body") or "{}").get("type")
    except ValueError:
        return None
    return problem_type


def _with_contract_headers(response: dict[str, Any], request_id: str) -> dict[str, Any]:
    extra = {**SECURITY_HEADERS, REQUEST_ID_HEADER: request_id}
    if "multiValueHeaders" in response:
        multi = response["multiValueHeaders"] or {}
        multi.update({name: [value] for name, value in extra.items()})
        response["multiValueHeaders"] = multi
    else:
        response["headers"] = {**(response.get("headers") or {}), **extra}
    return response


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    request_id = (event.get("requestContext") or {}).get("requestId", "")
    logger.append_keys(requestId=request_id, env=os.environ.get("ORDERS_API_ENV", "unknown"))
    started = time.perf_counter()
    try:
        response = app.resolve(event, context)
    except Exception:
        logger.exception("unhandled_error")
        response = _unexpected_error_response(event.get("path", ""), request_id)
    logger.info(
        "request",
        extra={
            "route": f"{event.get('httpMethod')} {event.get('resource')}",
            "status": response.get("statusCode"),
            "latencyMs": round((time.perf_counter() - started) * 1000, 1),
            "problemType": _problem_type(response),
        },
    )
    return _with_contract_headers(response, request_id)
