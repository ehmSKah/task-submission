"""Provider-agnostic OIDC access-token validation (ADR 0003).

No Lambda or API Gateway imports: the authorizer is a thin adapter over this module, and the
same code would run as middleware in a container. Everything provider-specific (issuer, the
access-token marker, the client claim, the roles claim) is configuration.

The nine checks run in order and the first failure rejects the token:
1. `Authorization: Bearer <token>` is present and well formed
2. the header's `alg` is on the allow-list and `kid` is present
3. `kid` matches a key in the provider's JWKS
4. the signature is valid
5. `iss` equals the configured issuer exactly
6. `exp` is in the future; `iat` and `nbf` (if present) are not in the future, with tolerance
7. the configured marker claim says it is an access token
8. the configured client claim is on the allow-list
9. `sub` is present
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import jwt

# Unverified values copied into a rejection are for logs only; this bounds their size.
MAX_LOGGED_LENGTH = 128


class Reason(StrEnum):
    """Why a token was rejected: logged, never returned to the caller."""

    MALFORMED_HEADER = "malformed_authorization_header"  # check 1
    MALFORMED_TOKEN = "malformed_token"  # check 1
    ALGORITHM_NOT_ALLOWED = "algorithm_not_allowed"  # check 2
    MISSING_KID = "missing_kid"  # check 2
    UNKNOWN_KID = "unknown_kid"  # check 3
    BAD_SIGNATURE = "bad_signature"  # check 4
    WRONG_ISSUER = "wrong_issuer"  # check 5
    EXPIRED = "expired"  # check 6
    NOT_YET_VALID = "not_yet_valid"  # check 6
    INVALID_TIME = "invalid_time_claims"  # check 6
    NOT_AN_ACCESS_TOKEN = "not_an_access_token"  # check 7
    CLIENT_NOT_ALLOWED = "client_not_allowed"  # check 8
    MISSING_SUBJECT = "missing_subject"  # check 9


class InvalidToken(Exception):
    """The token was rejected. Every field is for logs only, never for the caller.

    `kid`, `issuer` and `client_id` are read from the token without trusting it, so the logs can
    tell a foreign pool from an untrusted client from a forgery.
    """

    def __init__(
        self,
        reason: Reason,
        *,
        kid: str | None = None,
        issuer: str | None = None,
        client_id: str | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.kid = kid
        self.issuer = issuer
        self.client_id = client_id


class SigningKeySource(Protocol):
    def signing_key(self, kid: str) -> Any | None:
        """The public key for `kid`, or None if the provider doesn't publish it.

        Raises `SigningKeysUnavailable` when no keys are known at all.
        """
        ...


@dataclass(frozen=True)
class ValidatorConfig:
    """One identity provider's settings (ADRs 0001 and 0003)."""

    issuer: str
    # The access-token marker: Cognito `token_use=access`, Keycloak `typ=Bearer`.
    token_type_claim: str
    token_type_value: str
    # Who called: Cognito `client_id`; Keycloak and Entra ID `azp` / `aud`.
    client_claim: str
    allowed_clients: frozenset[str]
    # Cognito `cognito:groups`, Keycloak `realm_access.roles`, Okta `groups`, Entra ID `roles`.
    roles_claim: str
    # group -> role; None means the groups already carry the role names.
    group_roles: Mapping[str, str] | None = None
    # Overrides OIDC discovery when set.
    jwks_uri: str | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 30


@dataclass(frozen=True)
class Identity:
    subject: str
    roles: tuple[str, ...]
    client_id: str


def _loggable(value: Any) -> str | None:
    return value[:MAX_LOGGED_LENGTH] if isinstance(value, str) else None


def _claim(claims: Mapping[str, Any], name: str) -> Any:
    """A claim by name, or by dotted path into nested objects (`realm_access.roles`)."""
    if name in claims:
        return claims[name]
    value: Any = claims
    for part in name.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


class TokenValidator:
    def __init__(self, config: ValidatorConfig, keys: SigningKeySource) -> None:
        self._config = config
        self._keys = keys

    def validate(self, authorization: str | None) -> Identity:
        """Run the nine checks on an `Authorization` header value."""
        config = self._config
        token = self._bearer_token(authorization)  # 1

        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as error:
            raise InvalidToken(Reason.MALFORMED_TOKEN) from error
        kid = header.get("kid")

        def reject(reason: Reason) -> InvalidToken:
            return InvalidToken(
                reason,
                kid=_loggable(kid),
                issuer=_loggable(unverified.get("iss")),
                client_id=_loggable(_claim(unverified, config.client_claim)),
            )

        if header.get("alg") not in config.algorithms:  # 2
            raise reject(Reason.ALGORITHM_NOT_ALLOWED)
        if not isinstance(kid, str) or not kid:
            raise reject(Reason.MISSING_KID)

        key = self._keys.signing_key(kid)  # 3
        if key is None:
            raise reject(Reason.UNKNOWN_KID)

        # PyJWT checks the time before the issuer, so it runs twice to keep ADR 0003's order:
        # first the signature (4) and the issuer (5), then the time (6) on the verified token.
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(config.algorithms),
                issuer=config.issuer,
                options={
                    "require": ["iss"],
                    "verify_iss": True,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                    "verify_aud": False,
                    "verify_sub": False,
                    "verify_jti": False,
                },
            )
        except jwt.InvalidSignatureError as error:
            raise reject(Reason.BAD_SIGNATURE) from error
        except (jwt.InvalidIssuerError, jwt.MissingRequiredClaimError) as error:
            raise reject(Reason.WRONG_ISSUER) from error
        except jwt.PyJWTError as error:
            raise reject(Reason.MALFORMED_TOKEN) from error

        try:  # 6
            jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "require": ["exp"],
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                },
                leeway=config.leeway_seconds,
            )
        except jwt.ExpiredSignatureError as error:
            raise reject(Reason.EXPIRED) from error
        except jwt.ImmatureSignatureError as error:
            raise reject(Reason.NOT_YET_VALID) from error
        except jwt.PyJWTError as error:
            raise reject(Reason.INVALID_TIME) from error

        if _claim(claims, config.token_type_claim) != config.token_type_value:  # 7
            raise reject(Reason.NOT_AN_ACCESS_TOKEN)

        clients = _strings(_claim(claims, config.client_claim))  # 8
        client_id = next((c for c in clients if c in config.allowed_clients), None)
        if client_id is None:
            raise reject(Reason.CLIENT_NOT_ALLOWED)

        subject = claims.get("sub")  # 9
        if not isinstance(subject, str) or not subject:
            raise reject(Reason.MISSING_SUBJECT)

        return Identity(subject=subject, roles=self._roles(claims), client_id=client_id)

    @staticmethod
    def _bearer_token(authorization: str | None) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
            raise InvalidToken(Reason.MALFORMED_HEADER)
        return token

    def _roles(self, claims: Mapping[str, Any]) -> tuple[str, ...]:
        groups = _strings(_claim(claims, self._config.roles_claim))
        mapping = self._config.group_roles
        roles = groups if mapping is None else [mapping[g] for g in groups if g in mapping]
        return tuple(dict.fromkeys(roles))
