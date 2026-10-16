"""Every rejected token gets the same 401 from the gateway (ADRs 0003 and 0011).

The cases are the realistic shapes of a bad token: made by the test, forged, or real but wrong.
Unit tests isolate each of the nine checks; here the whole path proves the answer is one generic
`401`. Every token is new to the authorizer: API Gateway caches its decision per token for five
minutes, so a reused token could be answered from that cache instead.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

import boto3
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.demo_users import demo_password, demo_user
from orders_api_infra.identity import FIXTURE_OUTPUTS
from tests.deployment import FLOCI_URL
from tests.integration.assertions import assert_problem

ORDER_PATH = "/api/v1/orders/0e6bb2c6-0000-4000-8000-000000000000"
FOREIGN_USER = "foreign-user@example.test"
TOKEN_LEEWAY_SECONDS = 30  # ADR 0003

SignIn = Callable[[str], dict[str, str]]

_sent: set[str] = set()


def fresh(authorization: str) -> dict[str, str]:
    """Headers for a token this run hasn't sent before, so the authorizer really decides."""
    assert authorization not in _sent, "a reused token could be answered from the cache"
    _sent.add(authorization)
    return {"Authorization": authorization}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def segments(header: dict[str, Any], claims: dict[str, Any]) -> str:
    return f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"


def claims_of(token: str) -> dict[str, Any]:
    """A real token's claims, with a new `jti` so every forgery made from them is unique."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return {**claims, "jti": str(uuid.uuid4())}


@pytest.fixture(scope="module")
def config(endpoints: dict[str, Any]) -> EnvConfig:
    return load_config(endpoints["env"])


@pytest.fixture(scope="module")
def unauthorized_body(api: httpx.Client) -> dict[str, Any]:
    """The gateway's own answer to a request without a token: what every rejection must equal."""
    response = api.get(ORDER_PATH)
    assert_problem(response, 401, "unauthorized")
    body: dict[str, Any] = response.json()
    del body["requestId"]
    return body


@pytest.fixture
def assert_unauthorized(unauthorized_body: dict[str, Any]) -> Callable[[httpx.Response], None]:
    def check(response: httpx.Response) -> None:
        assert_problem(response, 401, "unauthorized")
        assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
        body = response.json()
        del body["requestId"]
        assert body == unauthorized_body

    return check


def sign_in_with(client_id: str, email: str, password: str) -> dict[str, str]:
    """A real sign-in: a new access and ID token on every call."""
    result = boto3.client("cognito-idp").initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    tokens = result["AuthenticationResult"]
    return {"AccessToken": tokens["AccessToken"], "IdToken": tokens["IdToken"]}


@pytest.fixture(scope="module")
def sign_in_alice(config: EnvConfig) -> SignIn:
    """Alice signs in through the given app client of our pool."""
    password = demo_password(config)
    email = demo_user("alice").email
    return lambda client_id: sign_in_with(client_id, email, password)


@pytest.fixture(scope="module")
def fixture_ids(endpoints: dict[str, Any]) -> dict[str, str]:
    keys = tuple(FIXTURE_OUTPUTS.values())
    if not all(key in endpoints for key in keys):
        pytest.skip("this environment has no token test fixtures (testFixtures: false)")
    return {key: endpoints[key] for key in keys}


@pytest.fixture(scope="module")
def pool_public_key(endpoints: dict[str, Any], config: EnvConfig) -> rsa.RSAPublicKey:
    """Our pool's published signing key, found the way the authorizer finds it."""

    def from_here(url: str) -> str:
        # On Floci the issuer names the host Lambda containers see; tests reach it locally.
        base = config.floci_issuer_base_url
        return url.replace(base, FLOCI_URL, 1) if base else url

    issuer = endpoints["tokenIssuer"]
    discovery = httpx.get(from_here(f"{issuer}/.well-known/openid-configuration")).json()
    jwks = httpx.get(from_here(discovery["jwks_uri"])).json()
    key = RSAAlgorithm.from_jwk(jwks["keys"][0])
    assert isinstance(key, rsa.RSAPublicKey)
    return key


def test_missing_token_is_rejected_by_the_gateway(
    api: httpx.Client, assert_unauthorized: Callable[[httpx.Response], None]
) -> None:
    assert_unauthorized(api.get(ORDER_PATH))


@pytest.mark.parametrize(
    "malformed",
    [
        lambda: f"Bearer garbage-{uuid.uuid4()}",
        lambda: f"Bearer {uuid.uuid4().hex}.{uuid.uuid4().hex}.{uuid.uuid4().hex}",
        lambda: f"Basic {b64(f'alice:{uuid.uuid4()}'.encode())}",
        lambda: f"{uuid.uuid4()}",
    ],
    ids=["not-a-jwt", "three-garbage-parts", "basic-scheme", "no-scheme"],
)
def test_malformed_header_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    malformed: Callable[[], str],
) -> None:
    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(malformed())))


def test_alg_none_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    endpoints: dict[str, Any],
) -> None:
    real = sign_in_alice(endpoints["userPoolClientId"])["AccessToken"]
    header = {**jwt.get_unverified_header(real), "alg": "none"}

    token = f"{segments(header, claims_of(real))}."

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {token}")))


def test_hs256_signed_with_the_pools_public_key_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    endpoints: dict[str, Any],
    pool_public_key: rsa.RSAPublicKey,
) -> None:
    real = sign_in_alice(endpoints["userPoolClientId"])["AccessToken"]
    header = {**jwt.get_unverified_header(real), "alg": "HS256"}
    secret = pool_public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    signing_input = segments(header, claims_of(real))
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()

    token = f"{signing_input}.{b64(signature)}"

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {token}")))


def test_real_claims_re_signed_with_our_own_key_are_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    endpoints: dict[str, Any],
) -> None:
    real = sign_in_alice(endpoints["userPoolClientId"])["AccessToken"]
    our_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    token = jwt.encode(
        claims_of(real), our_key, algorithm="RS256", headers=jwt.get_unverified_header(real)
    )

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {token}")))


def test_an_id_token_instead_of_an_access_token_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    endpoints: dict[str, Any],
) -> None:
    id_token = sign_in_alice(endpoints["userPoolClientId"])["IdToken"]
    assert claims_of(id_token)["token_use"] == "id"

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {id_token}")))


def test_a_token_from_a_foreign_user_pool_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    fixture_ids: dict[str, str],
    endpoints: dict[str, Any],
) -> None:
    pool_id = fixture_ids["fixtureForeignUserPoolId"]
    password = f"{secrets.token_urlsafe(18)}aA1!"  # a throwaway user; nothing is stored
    cognito = boto3.client("cognito-idp")
    with contextlib.suppress(cognito.exceptions.UsernameExistsException):
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=FOREIGN_USER,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": FOREIGN_USER}],
        )
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=FOREIGN_USER, Password=password, Permanent=True
    )

    token = sign_in_with(fixture_ids["fixtureForeignClientId"], FOREIGN_USER, password)
    access_token = token["AccessToken"]
    assert claims_of(access_token)["iss"] != endpoints["tokenIssuer"]

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {access_token}")))


def test_a_token_from_an_untrusted_client_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    fixture_ids: dict[str, str],
    endpoints: dict[str, Any],
) -> None:
    token = sign_in_alice(fixture_ids["fixtureUntrustedClientId"])["AccessToken"]
    claims = claims_of(token)
    assert claims["iss"] == endpoints["tokenIssuer"]
    assert claims["client_id"] == fixture_ids["fixtureUntrustedClientId"]

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {token}")))


@pytest.mark.floci_only
def test_an_expired_token_is_rejected(
    api: httpx.Client,
    assert_unauthorized: Callable[[httpx.Response], None],
    sign_in_alice: SignIn,
    fixture_ids: dict[str, str],
) -> None:
    client_id = fixture_ids["fixtureShortLivedClientId"]
    # Control: a fresh token from the same client is accepted, so only time can reject the next.
    control = sign_in_alice(client_id)["AccessToken"]
    accepted = api.get(ORDER_PATH, headers=fresh(f"Bearer {control}"))
    assert_problem(accepted, 404, "not-found")

    expiring = sign_in_alice(client_id)["AccessToken"]
    expires_at = claims_of(expiring)["exp"]
    assert expires_at - time.time() < 60, "the short-lived client issued a long-lived token"
    time.sleep(max(0.0, expires_at + TOKEN_LEEWAY_SECONDS + 3 - time.time()))

    assert_unauthorized(api.get(ORDER_PATH, headers=fresh(f"Bearer {expiring}")))
