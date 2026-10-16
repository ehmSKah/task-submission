from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from orders_api.auth.oidc import OidcKeySource

ISSUER = "http://floci:4566/us-east-1_orderspool"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"


def jwk(kid: str) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    public: dict[str, Any] = RSAAlgorithm.to_jwk(key, as_dict=True)
    return {**public, "kid": kid, "alg": "RS256", "use": "sig"}


class FakeHttp:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = documents
        self.requested: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.requested.append(url)
        return self.documents[url]


def test_keys_are_found_through_oidc_discovery() -> None:
    http = FakeHttp(
        {
            f"{ISSUER}/.well-known/openid-configuration": {"jwks_uri": JWKS_URI},
            JWKS_URI: {"keys": [jwk("pool-key")]},
        }
    )

    source = OidcKeySource(ISSUER, fetch_json=http)

    assert source.signing_key("pool-key") is not None
    assert source.signing_key("unknown") is None


def test_keys_are_fetched_once_and_reused() -> None:
    http = FakeHttp(
        {
            f"{ISSUER}/.well-known/openid-configuration": {"jwks_uri": JWKS_URI},
            JWKS_URI: {"keys": [jwk("pool-key")]},
        }
    )
    source = OidcKeySource(ISSUER, fetch_json=http)

    source.signing_key("pool-key")
    source.signing_key("pool-key")

    assert http.requested.count(JWKS_URI) == 1
