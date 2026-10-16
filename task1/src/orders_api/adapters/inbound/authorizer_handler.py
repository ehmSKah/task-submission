"""Lambda entry point for the REQUEST authorizer: a thin adapter over the token validator.

It answers "who is this?"; the API function decides "may they do this?" (ADRs 0001 and 0003).
"""

import os
from typing import Any, Protocol

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from orders_api.auth.oidc import JwksCache
from orders_api.auth.tokens import Identity, InvalidToken, TokenValidator, ValidatorConfig
from orders_api.domain.access import roles_from_groups

logger = Logger(service="orders-api")


class Validator(Protocol):
    def validate(self, token: str) -> Identity: ...


_validator: Validator | None = None


def validator() -> Validator:
    global _validator
    if _validator is None:
        issuer = os.environ["TOKEN_ISSUER"]
        _validator = TokenValidator(ValidatorConfig(issuer=issuer), JwksCache(issuer))
    return _validator


class Unauthorized(Exception):
    """API Gateway turns exactly this message into its UNAUTHORIZED response (401)."""

    def __init__(self) -> None:
        super().__init__("Unauthorized")


def _bearer_token(event: dict[str, Any]) -> str | None:
    headers = {name.lower(): value for name, value in (event.get("headers") or {}).items()}
    scheme, _, token = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _whole_api(method_arn: str) -> str:
    """`arn:...:<apiId>/<stage>/<verb>/<path>` -> `arn:...:<apiId>/<stage>/*`.

    The decision is cached per token, not per route, so the policy must cover the whole API.
    """
    api_arn, stage = method_arn.split("/")[:2]
    return f"{api_arn}/{stage}/*"


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    logger.append_keys(
        requestId=(event.get("requestContext") or {}).get("requestId", ""),
        env=os.environ.get("ORDERS_API_ENV", "unknown"),
        function="authorizer",
    )
    token = _bearer_token(event)
    if token is None:
        logger.info("token_rejected", extra={"reason": "missing or malformed header"})
        raise Unauthorized
    try:
        identity = validator().validate(token)
    except InvalidToken as rejection:
        logger.info("token_rejected", extra={"reason": rejection.reason})
        raise Unauthorized from rejection
    roles = sorted(roles_from_groups(identity.groups))
    logger.info("token_accepted", extra={"sub": identity.subject, "roles": roles})
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
