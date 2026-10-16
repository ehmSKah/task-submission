"""Signing keys from an OpenID Connect provider: discovery plus our own JWKS cache (ADR 0003).

PyJWT's `PyJWKClient` has neither discovery nor a limit on refetching, so the cache is ours:
- the JWKS location comes from `{issuer}/.well-known/openid-configuration` unless configured
- keys live in memory for one hour
- an unknown `kid` refetches, but at most once every 60 seconds, so random key ids can't make
  us hammer the provider; a failed refresh is retried on the same schedule
- each fetch gives up after 2 seconds
- when the provider is unreachable, the cached keys are used even when stale (keys are public
  and stay valid until rotated)
- with no keys at all, `SigningKeysUnavailable` is raised: the authorizer fails closed (`503`).
  While empty, every lookup tries again, so the service recovers as soon as the provider does.

Operational events (ADR 0012) go to an injected callback, so this module needs no logger.
"""

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

import jwt
from jwt import PyJWK

FETCH_TIMEOUT_SECONDS = 2.0
KEYS_TTL_SECONDS = 3600.0
REFETCH_INTERVAL_SECONDS = 60.0

JsonFetcher = Callable[[str], dict[str, Any]]
Clock = Callable[[], float]


class KeyFetchError(Exception):
    """The provider couldn't be reached in time, or its answer was unusable."""


class SigningKeysUnavailable(Exception):
    """No signing keys are known at all, so no token can be verified."""


class KeyEvents(Protocol):
    def __call__(self, event: str, /, **fields: object) -> None:
        """Report `jwks_fetched`, `jwks_refetch` or `jwks_fetch_failed` with its details."""
        ...


def no_events(event: str, /, **fields: object) -> None:
    """The default: report nothing."""


def fetch_json(url: str, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict[str, Any]:
    """GET a JSON object; any network, timeout or parse failure becomes `KeyFetchError`."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            document = json.load(response)
    except (OSError, ValueError) as error:
        raise KeyFetchError(f"{url}: {error}") from error
    if not isinstance(document, dict):
        raise KeyFetchError(f"{url}: not a JSON object")
    return document


def _usable_keys(jwks: dict[str, Any]) -> dict[str, Any]:
    """RSA signing keys by kid. Anything else can't verify RS256 and is ignored."""
    entries = jwks.get("keys")
    if not isinstance(entries, list):
        raise KeyFetchError("JWKS has no 'keys' list")
    keys: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid or entry.get("kty") != "RSA":
            continue
        if entry.get("use", "sig") != "sig":
            continue
        try:
            keys[kid] = PyJWK(entry).key
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
    return keys


class JwksCache:
    """The provider's signing keys by kid, cached in this instance's memory."""

    def __init__(
        self,
        issuer: str,
        *,
        jwks_uri: str | None = None,
        fetch_json: JsonFetcher = fetch_json,
        clock: Clock = time.monotonic,
        events: KeyEvents = no_events,
        ttl_seconds: float = KEYS_TTL_SECONDS,
        refetch_interval_seconds: float = REFETCH_INTERVAL_SECONDS,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._jwks_uri = jwks_uri
        self._fetch_json = fetch_json
        self._clock = clock
        self._events = events
        self._ttl = ttl_seconds
        self._refetch_interval = refetch_interval_seconds
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._last_attempt: float | None = None

    def signing_key(self, kid: str) -> Any | None:
        """The public key for `kid`, or None if the provider doesn't publish it."""
        now = self._clock()
        if not self._keys:
            self._refresh(now, "empty")
        elif now - self._fetched_at >= self._ttl and self._may_fetch(now):
            self._refresh(now, "expired")
        if not self._keys:
            raise SigningKeysUnavailable
        key = self._keys.get(kid)
        if key is None and self._may_fetch(now):
            self._refresh(now, "unknown_kid", kid=kid)
            key = self._keys.get(kid)
        return key

    def _may_fetch(self, now: float) -> bool:
        return self._last_attempt is None or now - self._last_attempt >= self._refetch_interval

    def _refresh(self, now: float, trigger: str, kid: str | None = None) -> None:
        self._last_attempt = now
        try:
            jwks_uri = self._discovered_jwks_uri()
            keys = _usable_keys(self._fetch_json(jwks_uri))
            if not keys:
                raise KeyFetchError("JWKS has no usable RS256 signing keys")
        except KeyFetchError as error:
            self._events(
                "jwks_fetch_failed",
                trigger=trigger,
                error=str(error),
                staleKeyCount=len(self._keys),
            )
            return
        self._keys = keys
        self._fetched_at = now
        details: dict[str, object] = {"trigger": trigger, "jwksUri": jwks_uri, "kids": sorted(keys)}
        if kid is None:
            self._events("jwks_fetched", **details)
        else:
            self._events("jwks_refetch", kid=kid, found=kid in keys, **details)

    def _discovered_jwks_uri(self) -> str:
        if self._jwks_uri is None:
            discovery = self._fetch_json(f"{self._issuer}/.well-known/openid-configuration")
            jwks_uri = discovery.get("jwks_uri")
            if not isinstance(jwks_uri, str) or not jwks_uri:
                raise KeyFetchError("discovery document has no jwks_uri")
            self._jwks_uri = jwks_uri
        return self._jwks_uri
