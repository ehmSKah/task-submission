"""Signing keys from an OpenID Connect provider, found through discovery (ADR 0003)."""

import json
import urllib.request
from collections.abc import Callable
from typing import Any

from jwt import PyJWK

FETCH_TIMEOUT_SECONDS = 2

JsonFetcher = Callable[[str], dict[str, Any]]


def fetch_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
        document: dict[str, Any] = json.load(response)
    return document


class OidcKeySource:
    """Looks up `{issuer}/.well-known/openid-configuration`, then caches the JWKS keys by kid."""

    def __init__(self, issuer: str, fetch_json: JsonFetcher = fetch_json) -> None:
        self._issuer = issuer.rstrip("/")
        self._fetch_json = fetch_json
        self._keys: dict[str, Any] | None = None

    def signing_key(self, kid: str) -> Any:
        if self._keys is None:
            self._keys = self._load_keys()
        return self._keys.get(kid)

    def _load_keys(self) -> dict[str, Any]:
        discovery = self._fetch_json(f"{self._issuer}/.well-known/openid-configuration")
        jwks = self._fetch_json(str(discovery["jwks_uri"]))
        return {jwk["kid"]: PyJWK(jwk).key for jwk in jwks.get("keys", []) if "kid" in jwk}
