"""Retried creates are safe: Powertools Idempotency around the create use case (ADRs 0004, 0009).

`POST /api/v1/orders` may carry an `Idempotency-Key`. The key is scoped per owner (`sub`), the
request is fingerprinted, and the serialised `201` response is kept for 24 hours in the
idempotency table (ADR 0005). A repeat with the same body replays that response; a different
body raises `IdempotencyValidationError`; a repeat while the first is still running raises
`IdempotencyAlreadyInProgressError`. Without the header the request runs normally.
"""

import dataclasses
import warnings
from collections.abc import Callable, Mapping
from typing import Any

from aws_lambda_powertools.event_handler import Response
from aws_lambda_powertools.utilities.idempotency import (
    BasePersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.idempotency.serialization.custom_dict import (
    CustomDictSerializer,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from orders_api.domain.order import NewOrder

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
EXPIRES_AFTER_SECONDS = 24 * 60 * 60
# Records are named after the operation, not the default (the Lambda function's name), so a
# redeploy that renames the function keeps them valid.
KEY_PREFIX = "createOrder"

_CONFIG = IdempotencyConfig(
    event_key_jmespath="idempotencyKey",
    payload_validation_jmespath="request",
    raise_on_no_idempotency_key=False,
    expires_after_seconds=EXPIRES_AFTER_SECONDS,
    hash_function="sha256",
)


def idempotency_key(headers: Mapping[str, str] | None) -> str | None:
    """The `Idempotency-Key` header in any casing: REST APIs pass the client's (ADR 0009)."""
    wanted = IDEMPOTENCY_KEY_HEADER.lower()
    return next((value for name, value in (headers or {}).items() if name.lower() == wanted), None)


def _response_to_dict(response: Response[str]) -> dict[str, Any]:
    return {"statusCode": response.status_code, "headers": response.headers, "body": response.body}


def _response_from_dict(stored: dict[str, Any]) -> Response[str]:
    return Response(
        status_code=stored["statusCode"], headers=stored["headers"], body=stored["body"]
    )


_RESPONSES = CustomDictSerializer(to_dict=_response_to_dict, from_dict=_response_from_dict)


def _run(*, create: Callable[[], Response[str]], payload: dict[str, Any]) -> Response[str]:
    return create()


def run_once(
    create: Callable[[], Response[str]],
    *,
    store: BasePersistenceLayer,
    lambda_context: LambdaContext,
    owner_id: str,
    key: str | None,
    request: NewOrder,
) -> Response[str]:
    """Run `create` at most once per owner and key; a repeat gets its stored response back.

    The record key hashes `{owner, key}`, so two owners' keys never meet. The fingerprint
    hashes the parsed request, so formatting and field order don't count as a different body.
    An in-progress record lasts no longer than this invocation can run.
    """
    _CONFIG.register_lambda_context(lambda_context)
    payload = {
        "idempotencyKey": {"owner": owner_id, "key": key} if key else None,
        "request": dataclasses.asdict(request),
    }
    idempotent_create = idempotent_function(
        _run,
        data_keyword_argument="payload",
        persistence_store=store,
        config=_CONFIG,
        output_serializer=_RESPONSES,
        key_prefix=KEY_PREFIX,
    )
    with warnings.catch_warnings():
        # A request without the header is normal here (ADR 0004), not a misconfiguration, so
        # Powertools' warning about a missing key would only be noise in the logs.
        warnings.filterwarnings("ignore", "No idempotency key value found", UserWarning)
        response: Response[str] = idempotent_create(create=create, payload=payload)
    return response
