"""Lambda entry point for the REQUEST authorizer: a thin adapter over the token validator.

It answers "who is this?"; the API function decides "may they do this?" (ADRs 0001 and 0003).
Every rejection becomes the same `401`; the precise reason, `kid`, `iss` and `client_id` are
only logged, and the token never is (ADR 0012). With no signing keys at all the function fails,
which API Gateway answers with `503`.
"""

import os
from collections.abc import Mapping
from typing import Any, Protocol

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from orders_api.auth.oidc import JwksCache, SigningKeysUnavailable
from orders_api.auth.tokens import Identity, InvalidToken, TokenValidator, ValidatorConfig
from orders_api.domain.access import roles_from_groups

logger = Logger(service="orders-api")


class Validator(Protocol):
    def validate(self, authorization: str | None) -> Identity: ...


_validator: Validator | None = None
# Whether this invocation fetched signing keys, for the decision line (ADR 0012).
_keys_fetched = False


def report_key_event(event: str, /, **fields: object) -> None:
    """Log the key cache's `jwks_*` events; a failed fetch is a warning."""
    global _keys_fetched
    if event == "jwks_fetch_failed":
        logger.warning(event, extra=fields)
    else:
        _keys_fetched = True
        logger.info(event, extra=fields)


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def config_from_environment(environ: Mapping[str, str]) -> ValidatorConfig:
    """The identity provider's settings; a missing one fails closed (`KeyError` -> `503`)."""
    group_roles = dict(pair.split("=", 1) for pair in _items(environ.get("TOKEN_GROUP_ROLES", "")))
    return ValidatorConfig(
        issuer=environ["TOKEN_ISSUER"],
        jwks_uri=environ.get("TOKEN_JWKS_URI") or None,
        token_type_claim=environ["TOKEN_TYPE_CLAIM"],
        token_type_value=environ["TOKEN_TYPE_VALUE"],
        client_claim=environ["TOKEN_CLIENT_CLAIM"],
        allowed_clients=frozenset(_items(environ["TOKEN_ALLOWED_CLIENTS"])),
        roles_claim=environ["TOKEN_ROLES_CLAIM"],
        group_roles={group.strip(): role.strip() for group, role in group_roles.items()} or None,
    )


def validator() -> Validator:
    global _validator
    if _validator is None:
        config = config_from_environment(os.environ)
        keys = JwksCache(config.issuer, jwks_uri=config.jwks_uri, events=report_key_event)
        _validator = TokenValidator(config, keys)
    return _validator


class Unauthorized(Exception):
    """API Gateway turns exactly this message into its UNAUTHORIZED response (401)."""

    def __init__(self) -> None:
        super().__init__("Unauthorized")


def _authorization(event: dict[str, Any]) -> str | None:
    headers = event.get("headers") or {}
    return next((v for k, v in headers.items() if k.lower() == "authorization"), None)


def _whole_api(method_arn: str) -> str:
    """`arn:...:<apiId>/<stage>/<verb>/<path>` -> `arn:...:<apiId>/<stage>/*`.

    The decision is cached per token, not per route, so the policy must cover the whole API.
    """
    api_arn, stage = method_arn.split("/")[:2]
    return f"{api_arn}/{stage}/*"


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    global _keys_fetched
    _keys_fetched = False
    logger.append_keys(
        requestId=(event.get("requestContext") or {}).get("requestId", ""),
        env=os.environ.get("ORDERS_API_ENV", "unknown"),
        function="authorizer",
    )
    try:
        identity = validator().validate(_authorization(event))
    except InvalidToken as rejection:
        logger.info(
            "token_rejected",
            extra={
                "reason": rejection.reason.value,
                "kid": rejection.kid,
                "iss": rejection.issuer,
                "client_id": rejection.client_id,
                "keysFetched": _keys_fetched,
            },
        )
        raise Unauthorized from rejection
    except SigningKeysUnavailable:
        logger.error("signing_keys_unavailable")
        raise
    roles = sorted(roles_from_groups(identity.roles))
    logger.info(
        "token_accepted",
        extra={
            "sub": identity.subject,
            "roles": roles,
            "client_id": identity.client_id,
            "keysFetched": _keys_fetched,
        },
    )
    return {
        "principalId": identity.subject,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": _whole_api(event["methodArn"]),
                }
            ],
        },
        # Authorizer context carries scalars only, so roles travel comma-separated.
        "context": {"sub": identity.subject, "roles": ",".join(roles)},
    }
