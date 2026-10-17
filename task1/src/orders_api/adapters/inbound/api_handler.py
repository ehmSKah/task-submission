"""Lambda entry point for the API function: routing and the HTTP response contract (ADR 0009)."""

import json
import os
import time
from contextvars import ContextVar
from functools import cache
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response
from aws_lambda_powertools.utilities.idempotency import BasePersistenceLayer
from aws_lambda_powertools.utilities.idempotency.exceptions import (
    IdempotencyAlreadyInProgressError,
    IdempotencyValidationError,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from orders_api.adapters.inbound.caller import MissingCallerIdentity, caller_from_event
from orders_api.adapters.inbound.http_headers import (
    JSON_MEDIA_TYPE,
    PROBLEM_JSON_MEDIA_TYPE,
    REQUEST_ID_HEADER,
    SECURITY_HEADERS,
)
from orders_api.adapters.inbound.idempotency import idempotency_key, run_once
from orders_api.adapters.inbound.order_json import order_to_json
from orders_api.adapters.inbound.order_list import order_list_query, order_page_to_json
from orders_api.adapters.inbound.order_requests import (
    ORDERS_PATH,
    if_match_versions,
    new_order_from,
    requested_status_from,
    valid_order_id,
)
from orders_api.adapters.inbound.problems import FieldError, ProblemType, problem_body
from orders_api.adapters.inbound.rate_limiting import (
    RETRY_AFTER_HEADER,
    MissingSourceIp,
    RateLimitExceeded,
    client_ip,
    rate_limit_headers,
    report_rate_limiter_event,
)
from orders_api.adapters.inbound.request_checks import InvalidRequest
from orders_api.adapters.outbound.dynamodb_idempotency import dynamodb_store
from orders_api.adapters.outbound.dynamodb_orders import DynamoDbOrderRepository
from orders_api.adapters.outbound.dynamodb_rate_limits import DynamoDbRateLimitCounters
from orders_api.adapters.outbound.ssm_rate_limits import SsmRateLimitConfig
from orders_api.adapters.outbound.system import RandomOrderIds, SystemClock
from orders_api.application.fail_open_counters import FailOpenCounterStore
from orders_api.application.orders import (
    Clock,
    IdGenerator,
    OrderNotFound,
    OrderRepository,
    VersionMismatch,
    change_order_status,
    check_may_change_status,
    check_may_create,
    check_may_delete,
    check_may_read,
    create_order,
    delete_order,
    get_order,
    list_orders,
    listed_owner,
)
from orders_api.application.rate_limiting import RateLimiter
from orders_api.domain.order import Order, StatusChangeNotAllowed
from orders_api.domain.permissions import Caller, PermissionDenied
from orders_api.domain.rate_limits import RateLimitDecision, RateLimitScope, logged_network

UNEXPECTED_ERROR_DETAIL = "An unexpected error occurred"

logger = Logger(service="orders-api")
app = APIGatewayRestResolver()

clock: Clock = SystemClock()
order_ids: IdGenerator = RandomOrderIds()

# The adapters each Lambda instance needs are made on first use and then kept (`cache` on a
# function without arguments), so a cold start pays only for what its request touches. Unit
# tests replace these functions with their own fakes.


@cache
def orders() -> OrderRepository:
    return DynamoDbOrderRepository(os.environ["ORDERS_TABLE"])


@cache
def idempotency_store() -> BasePersistenceLayer:
    return dynamodb_store(os.environ["IDEMPOTENCY_TABLE"])


@cache
def rate_limiter() -> RateLimiter:
    config = SsmRateLimitConfig(
        os.environ["RATE_LIMITS_PARAMETER"],
        max_age_seconds=int(os.environ["RATE_LIMITS_MAX_AGE_SECONDS"]),
    )
    counters = FailOpenCounterStore(
        DynamoDbRateLimitCounters(os.environ["RATE_LIMIT_TABLE"]), events=report_rate_limiter_event
    )
    return RateLimiter(counters, config, clock)


# This request's rate-limit decision, once step 3 has made it: every response from then on, an
# error included, carries its `RateLimit-*` headers (ADR 0006).
_rate_limit: ContextVar[RateLimitDecision | None] = ContextVar("rate_limit", default=None)


def health_status() -> dict[str, str]:
    """Shallow liveness: the function runs. No dependency is called (ADR 0002)."""
    return {"status": "ok"}


@app.get("/api/v1/health")
def get_health() -> Response[str]:
    return Response(status_code=200, content_type=JSON_MEDIA_TYPE, body=json.dumps(health_status()))


def admitted_caller() -> Caller:
    """Steps 2 and 3 of the request pipeline (ADR 0009): the authorizer's identity, or fail
    closed; then the rate limits, per IP and per user, or `429`."""
    caller = caller_from_event(app.current_event.raw_event)
    logger.append_keys(sub=caller.subject, roles=sorted(caller.roles))
    ip = client_ip(app.current_event.raw_event)
    decision = rate_limiter().check(ip, caller.subject, caller.roles)
    _rate_limit.set(decision)
    if not decision.allowed:
        logger.append_keys(rateLimitScope=decision.usage.scope, clientNetwork=logged_network(ip))
        raise RateLimitExceeded(decision)
    return caller


def requested_order_id(order_id: str) -> str:
    """Step 4 for the path `{id}`: a UUID or a `400`. Logged from here on, so every outcome
    for the order, `404`, `409` or `412` included, carries its id (ADR 0012)."""
    order_id = valid_order_id(order_id)
    logger.append_keys(orderId=order_id)
    return order_id


def order_response(order: Order, status_code: int) -> Response[str]:
    """An order in its published shape, with its version as the `ETag` (ADR 0004)."""
    logger.append_keys(orderId=order.id)
    return Response(
        status_code=status_code,
        content_type=JSON_MEDIA_TYPE,
        body=json.dumps(order_to_json(order)),
        headers={"ETag": f'"{order.version}"'},
    )


@app.post(ORDERS_PATH)
def post_order() -> Response[str]:
    caller = admitted_caller()
    request = new_order_from(app.current_event)
    # Step 6 before step 7: a caller without the permission never creates an idempotency record.
    check_may_create(caller)

    def create() -> Response[str]:
        order = create_order(orders(), clock, order_ids, owner_id=caller.subject, request=request)
        return order_response(order, 201)

    # Step 7: a retry with the same Idempotency-Key replays the first 201 (ADR 0009).
    return run_once(
        create,
        store=idempotency_store(),
        lambda_context=app.lambda_context,
        owner_id=caller.subject,
        key=idempotency_key(app.current_event.headers),
        request=request,
    )


@app.get("/api/v1/orders/<order_id>")
def get_order_by_id(order_id: str) -> Response[str]:
    caller = admitted_caller()
    order_id = requested_order_id(order_id)
    check_may_read(caller)
    return order_response(get_order(orders(), caller, order_id), 200)


@app.get(ORDERS_PATH)
def get_orders() -> Response[str]:
    caller = admitted_caller()
    query = order_list_query(app.current_event)
    # Step 6. Which list the caller sees is known only now, so a cursor's binding is checked here.
    owner_id = listed_owner(caller)
    page = list_orders(orders(), owner_id, limit=query.limit, after=query.start_in(owner_id))
    return Response(
        status_code=200,
        content_type=JSON_MEDIA_TYPE,
        body=json.dumps(order_page_to_json(page, owner_id)),
    )


@app.patch("/api/v1/orders/<order_id>/status")
def patch_order_status(order_id: str) -> Response[str]:
    caller = admitted_caller()
    order_id = requested_order_id(order_id)
    status = requested_status_from(app.current_event)
    # Step 6 needs the body: cancelling and every other status need different permissions.
    check_may_change_status(caller, status)
    order = change_order_status(
        orders(),
        clock,
        caller,
        order_id,
        status,
        if_match=if_match_versions(app.current_event),
    )
    return order_response(order, 200)


@app.delete("/api/v1/orders/<order_id>")
def delete_order_by_id(order_id: str) -> Response[str]:
    caller = admitted_caller()
    order_id = requested_order_id(order_id)
    check_may_delete(caller)
    delete_order(orders(), order_id)
    return Response(status_code=204)


def problem_response(
    problem: ProblemType,
    detail: str | None = None,
    errors: tuple[FieldError, ...] = (),
    *,
    scope: RateLimitScope | None = None,
    headers: dict[str, str] | None = None,
) -> Response[str]:
    body = problem_body(
        problem,
        detail=detail,
        instance=app.current_event.path,
        request_id=app.current_event.request_context.request_id,
        errors=errors,
        scope=scope,
    )
    return Response(
        status_code=problem.status,
        content_type=PROBLEM_JSON_MEDIA_TYPE,
        body=json.dumps(body),
        headers=headers,
    )


@app.not_found
def not_found(_: Exception) -> Response[str]:
    return problem_response(ProblemType.NOT_FOUND)


@app.exception_handler(InvalidRequest)  # type: ignore[untyped-decorator]
def invalid_request(error: InvalidRequest) -> Response[str]:
    return problem_response(error.problem, error.detail, error.errors)


@app.exception_handler(RateLimitExceeded)  # type: ignore[untyped-decorator]
def rate_limit_exceeded(error: RateLimitExceeded) -> Response[str]:
    """Names the limit that was hit; the `RateLimit-*` headers are added with every other one."""
    return problem_response(
        ProblemType.RATE_LIMITED,
        error.detail,
        scope=error.scope,
        headers={RETRY_AFTER_HEADER: str(error.decision.reset_seconds)},
    )


@app.exception_handler(PermissionDenied)  # type: ignore[untyped-decorator]
def permission_denied(error: PermissionDenied) -> Response[str]:
    """Names only the caller's own missing permission; says nothing about any order."""
    needed = " or ".join(sorted(error.needed))
    return problem_response(ProblemType.FORBIDDEN, f"Missing permission: {needed}")


@app.exception_handler(OrderNotFound)  # type: ignore[untyped-decorator]  # Powertools leaves it untyped
def order_not_found(_: OrderNotFound) -> Response[str]:
    return problem_response(ProblemType.NOT_FOUND)


@app.exception_handler(StatusChangeNotAllowed)  # type: ignore[untyped-decorator]
def status_change_not_allowed(error: StatusChangeNotAllowed) -> Response[str]:
    """Only reached once the caller may see the order, so naming its status leaks nothing."""
    return problem_response(ProblemType.CONFLICT, str(error))


@app.exception_handler(VersionMismatch)  # type: ignore[untyped-decorator]
def version_mismatch(_: VersionMismatch) -> Response[str]:
    return problem_response(
        ProblemType.PRECONDITION_FAILED, "If-Match doesn't name the order's current ETag"
    )


@app.exception_handler(IdempotencyValidationError)  # type: ignore[untyped-decorator]
def idempotency_key_mismatch(_: IdempotencyValidationError) -> Response[str]:
    return problem_response(
        ProblemType.IDEMPOTENCY_KEY_MISMATCH,
        "This Idempotency-Key was already used with a different request body",
    )


@app.exception_handler(IdempotencyAlreadyInProgressError)  # type: ignore[untyped-decorator]
def idempotent_request_in_progress(_: IdempotencyAlreadyInProgressError) -> Response[str]:
    return problem_response(
        ProblemType.CONFLICT,
        "A request with this Idempotency-Key is still in progress; retry it later",
    )


@app.exception_handler(MissingCallerIdentity)  # type: ignore[untyped-decorator]
def missing_caller_identity(_: MissingCallerIdentity) -> Response[str]:
    logger.error("authorizer_context_missing")
    return problem_response(ProblemType.INTERNAL_ERROR, UNEXPECTED_ERROR_DETAIL)


@app.exception_handler(MissingSourceIp)  # type: ignore[untyped-decorator]
def missing_source_ip(_: MissingSourceIp) -> Response[str]:
    logger.error("source_ip_missing")
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


def _with_contract_headers(
    response: dict[str, Any], request_id: str, rate_limit: RateLimitDecision | None
) -> dict[str, Any]:
    extra = {**SECURITY_HEADERS, REQUEST_ID_HEADER: request_id}
    if rate_limit is not None:
        extra.update(rate_limit_headers(rate_limit))
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
    _rate_limit.set(None)
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
    return _with_contract_headers(response, request_id, _rate_limit.get())
