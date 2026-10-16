"""Provider-agnostic OIDC access-token validation (ADR 0003).

No Lambda or API Gateway imports: the authorizer is a thin adapter over this module, and the
same code would run as middleware in a container.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt


class InvalidToken(Exception):
    """The token was rejected. The reason is for logs only, never for the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SigningKeySource(Protocol):
    def signing_key(self, kid: str) -> Any:
        """The public key for `kid`, or None if the provider doesn't publish it."""
        ...


@dataclass(frozen=True)
class ValidatorConfig:
    issuer: str
    algorithms: tuple[str, ...] = ("RS256",)
    roles_claim: str = "cognito:groups"
    leeway_seconds: int = 30


@dataclass(frozen=True)
class Identity:
    subject: str
    groups: tuple[str, ...] = field(default_factory=tuple)


class TokenValidator:
    def __init__(self, config: ValidatorConfig, keys: SigningKeySource) -> None:
        self._config = config
        self._keys = keys

    def validate(self, token: str) -> Identity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise InvalidToken("malformed token") from error
        key = self._keys.signing_key(str(header.get("kid", "")))
        if key is None:
            raise InvalidToken("unknown signing key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self._config.algorithms),
                issuer=self._config.issuer,
                leeway=self._config.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as error:
            raise InvalidToken("expired token") from error
        except jwt.InvalidIssuerError as error:
            raise InvalidToken("wrong issuer") from error
        except jwt.InvalidSignatureError as error:
            raise InvalidToken("bad signature") from error
        except jwt.PyJWTError as error:
            raise InvalidToken(f"invalid token: {error}") from error
        groups = claims.get(self._config.roles_claim) or []
        return Identity(subject=str(claims["sub"]), groups=tuple(str(g) for g in groups))
