"""The fulfilment stub: the fulfilment service's contract, plus a control side for tests (ADR 0003).

Requests are checked against the OpenAPI file, the contract's only definition, so a `400` from the
stub means the consumer's mapping produced something the contract doesn't allow. Each request to
the contract side continues the caller's trace (ADR 0008).
"""

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Self

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from openapi_core import OpenAPI
from openapi_core.contrib.starlette import StarletteOpenAPIRequest
from openapi_core.exceptions import OpenAPIError
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import TracerProvider
from pydantic import BaseModel, Field, model_validator
from starlette.types import Receive, Scope, Send

log = structlog.get_logger()


@dataclass
class IdempotencyRecord:
    """What the stub remembers per `Idempotency-Key` (ADR 0003). It never forgets until reset.

    Without a response the first request is still in progress.
    """

    fingerprint: str
    response: dict[str, str] | None = None


@dataclass
class FaultRule:
    """A fault for one order, applied to its next `times` requests (ADR 0003).

    The request claims its key, waits `delay_seconds`, then either answers with `status` or drops
    the connection, creating nothing, or, with neither, is processed as usual. A delay on its own
    therefore creates the order but answers late: a client that timed out meanwhile gets `409`
    on its repeat, and the `201` replay once the delay is over.
    """

    external_reference: str
    times: int
    delay_seconds: float = 0.0
    status: int | None = None
    retry_after: str | None = None
    drop_connection: bool = False

    @property
    def fails(self) -> bool:
        return self.status is not None or self.drop_connection

    def describe(self) -> dict[str, Any]:
        """The rule as the journal shows it: only what it changes."""
        fields = {
            "delaySeconds": self.delay_seconds or None,
            "status": self.status,
            "retryAfter": self.retry_after,
            "dropConnection": self.drop_connection or None,
        }
        return {name: value for name, value in fields.items() if value is not None}


class FaultRuleRequest(BaseModel):
    """`POST /_control/fault-rules`."""

    externalReference: str
    times: int = Field(default=1, ge=1)
    delaySeconds: float = Field(default=0.0, ge=0.0, le=60.0)
    status: int | None = Field(default=None, ge=200, le=599)
    retryAfter: str | None = Field(default=None, min_length=1, max_length=64)
    dropConnection: bool = False

    @model_validator(mode="after")
    def one_fault(self) -> Self:
        if self.status == HTTPStatus.CREATED:
            raise ValueError("a 201 is the stub's own answer; omit status to create the order")
        if self.status is not None and self.dropConnection:
            raise ValueError("answer with a status or drop the connection, not both")
        if self.retryAfter is not None and self.status is None:
            raise ValueError("retryAfter needs a status to send it with")
        return self


class DroppedConnection(Response):
    """Starts the answer and never completes it: the server closes the connection, and the
    client sees it dropped mid-answer. Nothing it receives is a usable response."""

    def __init__(self) -> None:
        super().__init__(status_code=HTTPStatus.SERVICE_UNAVAILABLE)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": [(b"content-length", b"1")],
            }
        )


@dataclass
class FaultRules:
    rules: list[FaultRule] = field(default_factory=list)

    def add(self, rule: FaultRule) -> None:
        self.rules.append(rule)

    def reset(self) -> None:
        self.rules.clear()

    def clear(self, external_reference: str) -> None:
        self.rules = [rule for rule in self.rules if rule.external_reference != external_reference]

    def take(self, external_reference: str) -> FaultRule | None:
        """The first rule for the order, with one of its uses spent."""
        for rule in self.rules:
            if rule.external_reference == external_reference:
                rule.times -= 1
                if rule.times == 0:
                    self.rules.remove(rule)
                return rule
        return None


@dataclass
class Journal:
    requests: list[dict[str, Any]] = field(default_factory=list)
    fulfilment_orders: list[dict[str, str]] = field(default_factory=list)

    def reset(self) -> None:
        self.requests.clear()
        self.fulfilment_orders.clear()


def fingerprint(body: bytes) -> str:
    """The same order with other whitespace or key order is the same body."""
    canonical = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def problem(
    status: int, title: str, headers: dict[str, str] | None = None, **extra: Any
) -> JSONResponse:
    return JSONResponse(
        {"title": title, "status": status, **extra},
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def injected_answer(rule: FaultRule) -> Response:
    """The answer a status rule asks for, for any code, known or not. Some statuses must not
    carry a body."""
    assert rule.status is not None
    headers = {"Retry-After": rule.retry_after} if rule.retry_after is not None else None
    if rule.status in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED):
        return Response(status_code=rule.status, headers=headers)
    try:
        title = HTTPStatus(rule.status).phrase
    except ValueError:
        title = "Injected Fault"
    return problem(rule.status, title, headers, detail="Injected fault.")


def contract_violations(error: OpenAPIError) -> dict[str, Any]:
    """What broke the contract, without the values: openapi-core's own message quotes the whole
    request body, which must reach neither the logs nor the response (ADR 0008)."""
    schema_errors = getattr(error.__cause__, "schema_errors", None)
    if not schema_errors:
        # Not about the body (e.g. a missing header): these messages carry no request values.
        return {"detail": str(error)}
    return {
        "detail": "The request body does not match the contract.",
        "errors": [
            {
                "pointer": "".join(f"/{part}" for part in schema_error.absolute_path),
                "rule": schema_error.validator,
            }
            for schema_error in schema_errors
        ],
    }


def create_app(
    spec_path: Path,
    token: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    """`sleep` is how a delay rule waits; tests replace it to hold a request in progress. Spans go
    to `tracer_provider`, or the global one."""
    openapi = OpenAPI.from_file_path(str(spec_path))
    journal = Journal()
    idempotency_records: dict[str, IdempotencyRecord] = {}
    fault_rules = FaultRules()
    app = FastAPI(title="Fulfilment stub", openapi_url=None, docs_url=None, redoc_url=None)
    # One server span per contract request. Health checks and the control side would only bury
    # the handovers; headers are not recorded.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider,
        excluded_urls="/health,/_control",
        exclude_spans=["receive", "send"],
    )

    def bearer_matches(request: Request) -> bool:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(presented, token)

    @app.post("/fulfilment-orders")
    async def create_fulfilment_order(request: Request) -> Response:
        body = await request.body()
        entry: dict[str, Any] = {
            "receivedAt": datetime.now(UTC).isoformat(),
            "idempotencyKey": request.headers.get("idempotency-key"),
            "traceparent": request.headers.get("traceparent"),
            "externalReference": None,
            "total": None,  # what the order's money mapping sent, once the body is valid
            "status": None,  # until the request is answered; never if its connection is dropped
            "fault": None,  # the rule applied to it
        }
        journal.requests.append(entry)

        if not bearer_matches(request):
            entry["status"] = 401
            return problem(401, "Unauthorized")
        try:
            openapi.validate_request(StarletteOpenAPIRequest(request, body))
        except OpenAPIError as error:
            entry["status"] = 400
            violations = contract_violations(error)
            log.info("request.invalid", **violations)
            return problem(400, "Bad Request", **violations)

        payload = json.loads(body)
        external_reference = payload["externalReference"]
        entry["externalReference"] = external_reference
        entry["total"] = payload["total"]
        rule = fault_rules.take(external_reference)
        if rule is not None:
            entry["fault"] = rule.describe()
        key = request.headers["idempotency-key"]
        body_fingerprint = fingerprint(body)
        record = idempotency_records.get(key)
        claimed = record is None
        if record is None:
            # Claimed: until this request is answered, a repeat with the same key gets 409.
            record = idempotency_records[key] = IdempotencyRecord(body_fingerprint)
        try:
            if rule is not None:
                await sleep(rule.delay_seconds)
        except BaseException:
            if claimed:
                idempotency_records.pop(key, None)
            raise
        if rule is not None and rule.fails:
            if claimed:
                del idempotency_records[key]  # nothing was created, so nothing is remembered
            if rule.drop_connection:
                return DroppedConnection()
            entry["status"] = rule.status
            return injected_answer(rule)
        if not claimed:
            return answer_repeat(record, body_fingerprint, entry)

        fulfilment_order = {
            "id": f"ful-{len(journal.fulfilment_orders) + 1}",
            "externalReference": external_reference,
        }
        journal.fulfilment_orders.append(fulfilment_order)
        record.response = fulfilment_order
        entry["status"] = 201
        log.info("fulfilment_order.created", **fulfilment_order)
        return JSONResponse(fulfilment_order, status_code=201)

    def answer_repeat(
        known: IdempotencyRecord, body_fingerprint: str, entry: dict[str, Any]
    ) -> Response:
        """The replay rules of the IETF Idempotency-Key draft (ADR 0003)."""
        if known.fingerprint != body_fingerprint:
            entry["status"] = 422
            return problem(
                422,
                "Unprocessable Content",
                detail="This Idempotency-Key was used with a different request body.",
            )
        if known.response is None:
            entry["status"] = 409
            return problem(
                409, "Conflict", detail="The first request with this key is still in progress."
            )
        entry["status"] = 201
        return JSONResponse(known.response, status_code=201)

    @app.post("/_control/fault-rules", status_code=201)
    def add_fault_rule(rule: FaultRuleRequest) -> FaultRuleRequest:
        fault_rules.add(
            FaultRule(
                rule.externalReference,
                rule.times,
                delay_seconds=rule.delaySeconds,
                status=rule.status,
                retry_after=rule.retryAfter,
                drop_connection=rule.dropConnection,
            )
        )
        return rule

    @app.delete("/_control/fault-rules", status_code=204)
    def clear_fault_rules(externalReference: str) -> None:
        """The order's remaining faults, as if the fulfilment service had recovered for it."""
        fault_rules.clear(externalReference)

    @app.get("/_control/requests")
    def requests(externalReference: str | None = None) -> list[dict[str, Any]]:
        return [
            entry
            for entry in journal.requests
            if externalReference is None or entry["externalReference"] == externalReference
        ]

    @app.get("/_control/fulfilment-orders")
    def fulfilment_orders(externalReference: str | None = None) -> list[dict[str, str]]:
        return [
            order
            for order in journal.fulfilment_orders
            if externalReference is None or order["externalReference"] == externalReference
        ]

    @app.delete("/_control", status_code=204)
    def reset() -> None:
        journal.reset()
        idempotency_records.clear()
        fault_rules.reset()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
