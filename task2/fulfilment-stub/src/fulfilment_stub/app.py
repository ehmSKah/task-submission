"""The fulfilment stub: the fulfilment service's contract, plus a control side for tests (ADR 0003).

Requests are checked against the OpenAPI file, the contract's only definition, so a `400` from the
stub means the consumer's mapping produced something the contract doesn't allow.
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from openapi_core import OpenAPI
from openapi_core.contrib.starlette import StarletteOpenAPIRequest
from openapi_core.exceptions import OpenAPIError

log = structlog.get_logger()


@dataclass
class Journal:
    requests: list[dict[str, Any]] = field(default_factory=list)
    fulfilment_orders: list[dict[str, str]] = field(default_factory=list)
    created: int = 0

    def reset(self) -> None:
        self.requests.clear()
        self.fulfilment_orders.clear()
        self.created = 0


def problem(status: int, title: str, detail: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(spec_path: Path, token: str) -> FastAPI:
    openapi = OpenAPI.from_file_path(str(spec_path))
    journal = Journal()
    app = FastAPI(title="Fulfilment stub", openapi_url=None, docs_url=None, redoc_url=None)

    def bearer_matches(request: Request) -> bool:
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and secrets.compare_digest(presented, token)

    @app.post("/fulfilment-orders")
    async def create_fulfilment_order(request: Request) -> Response:
        body = await request.body()
        entry: dict[str, Any] = {
            "receivedAt": datetime.now(UTC).isoformat(),
            "idempotencyKey": request.headers.get("idempotency-key"),
            "externalReference": None,
        }
        journal.requests.append(entry)

        if not bearer_matches(request):
            entry["status"] = 401
            return problem(401, "Unauthorized")
        try:
            openapi.validate_request(StarletteOpenAPIRequest(request, body))
        except OpenAPIError as error:
            entry["status"] = 400
            log.info("request.invalid", error=str(error))
            return problem(400, "Bad Request", str(error))

        external_reference = json.loads(body)["externalReference"]
        entry["externalReference"] = external_reference
        journal.created += 1
        fulfilment_order = {"id": f"ful-{journal.created}", "externalReference": external_reference}
        journal.fulfilment_orders.append(fulfilment_order)
        entry["status"] = 201
        log.info("fulfilment_order.created", **fulfilment_order)
        return JSONResponse(fulfilment_order, status_code=201)

    @app.get("/_control/requests")
    def requests() -> list[dict[str, Any]]:
        return journal.requests

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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
