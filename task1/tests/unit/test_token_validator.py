"""Each of ADR 0003's nine checks on its own, with our own keys and a fake JWKS (ADR 0011).

Every rejection test breaks exactly one thing in an otherwise valid token, so a provider could
never produce most of them (e.g. a validly signed token with the wrong issuer).
"""

import base64
import json
import time
import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orders_api.auth.oidc import SigningKeysUnavailable
from orders_api.auth.tokens import (
    Identity,
    InvalidToken,
    Reason,
    TokenValidator,
    ValidatorConfig,
)

ISSUER = "http://floci:4566/us-east-1_orderspool"
KID = "us-east-1_orderspool"
CLIENT = "orders-api-client"
LEEWAY = 30


class FakeKeySource:
    def __init__(self, keys: dict[str, Any]) -> None:
        self.keys = keys
        self.looked_up: list[str] = []

    def signing_key(self, kid: str) -> Any:
        self.looked_up.append(kid)
        return self.keys.get(kid)


class NoKeys:
    def signing_key(self, kid: str) -> Any:
        raise SigningKeysUnavailable


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def other_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def cognito_config(**overrides: Any) -> ValidatorConfig:
    settings: dict[str, Any] = {
        "issuer": ISSUER,
        "token_type_claim": "token_use",
        "token_type_value": "access",
        "client_claim": "client_id",
        "allowed_clients": frozenset({CLIENT}),
        "roles_claim": "cognito:groups",
    }
    settings.update(overrides)
    return ValidatorConfig(**settings)


@pytest.fixture
def keys(private_key: rsa.RSAPrivateKey) -> FakeKeySource:
    return FakeKeySource({KID: private_key.public_key()})


@pytest.fixture
def validator(keys: FakeKeySource) -> TokenValidator:
    return TokenValidator(cognito_config(), keys)


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": "alice-sub",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 900,
        "token_use": "access",
        "client_id": CLIENT,
        "cognito:groups": ["USER"],
        "jti": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return {name: value for name, value in payload.items() if value is not None}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def rs256(key: rsa.RSAPrivateKey, header: dict[str, Any], payload: dict[str, Any]) -> str:
    """Signs by hand, so claims PyJWT would refuse to encode (e.g. a numeric `iss`) work too."""
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(payload).encode())}"
    signature = jwt.get_algorithm_by_name("RS256").sign(signing_input.encode(), key)
    return f"{signing_input}.{b64(signature)}"


def signed(key: rsa.RSAPrivateKey, payload: dict[str, Any], **header: Any) -> str:
    return rs256(key, {"alg": "RS256", "typ": "JWT", "kid": KID, **header}, payload)


def bearer(token: str) -> str:
    return f"Bearer {token}"


def unsigned(header: dict[str, Any], payload: dict[str, Any]) -> str:
    return f"{b64(json.dumps(header).encode())}.{b64(json.dumps(payload).encode())}."


def rejection(validator: TokenValidator, authorization: str | None) -> InvalidToken:
    with pytest.raises(InvalidToken) as raised:
        validator.validate(authorization)
    return raised.value


# A valid token passes all nine checks.


def test_valid_token_yields_subject_roles_and_client(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(**{"cognito:groups": ["USER", "ADMIN"]}))

    identity = validator.validate(bearer(token))

    assert identity == Identity(subject="alice-sub", roles=("USER", "ADMIN"), client_id=CLIENT)


# Check 1: the header is `Authorization: Bearer <token>`.


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Bearer    ", "token-1", "Basic YWxpY2U6cHc=", "Bearer a b"],
)
def test_1_missing_or_malformed_header_is_rejected(
    validator: TokenValidator, keys: FakeKeySource, header: str | None
) -> None:
    assert rejection(validator, header).reason is Reason.MALFORMED_HEADER
    assert keys.looked_up == []


def test_1_the_scheme_is_case_insensitive(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims())

    assert validator.validate(f"bearer {token}").subject == "alice-sub"


@pytest.mark.parametrize("garbage", ["not-a-jwt", "a.b.c", "e30.e30"])
def test_1_a_token_that_is_not_a_jwt_is_rejected(validator: TokenValidator, garbage: str) -> None:
    assert rejection(validator, bearer(garbage)).reason is Reason.MALFORMED_TOKEN


# Check 2: `alg` is on the allow-list (RS256 only) and `kid` is present.


def test_2_alg_none_is_rejected(validator: TokenValidator, keys: FakeKeySource) -> None:
    token = unsigned({"alg": "none", "typ": "JWT", "kid": KID}, claims())

    assert rejection(validator, bearer(token)).reason is Reason.ALGORITHM_NOT_ALLOWED
    assert keys.looked_up == []


def test_2_hs256_signed_with_the_public_key_is_rejected(
    validator: TokenValidator, keys: FakeKeySource, private_key: rsa.RSAPrivateKey
) -> None:
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = b64(json.dumps(claims()).encode())
    signature = jwt.get_algorithm_by_name("HS256").sign(f"{header}.{payload}".encode(), public_pem)
    token = f"{header}.{payload}.{b64(signature)}"

    assert rejection(validator, bearer(token)).reason is Reason.ALGORITHM_NOT_ALLOWED
    assert keys.looked_up == []


def test_2_other_asymmetric_algorithms_are_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = jwt.encode(claims(), private_key, algorithm="PS256", headers={"kid": KID})

    assert rejection(validator, bearer(token)).reason is Reason.ALGORITHM_NOT_ALLOWED


@pytest.mark.parametrize("header", [{"alg": "RS256"}, {"alg": "RS256", "kid": ""}])
def test_2_a_missing_kid_is_rejected(
    validator: TokenValidator,
    keys: FakeKeySource,
    private_key: rsa.RSAPrivateKey,
    header: dict[str, Any],
) -> None:
    token = rs256(private_key, header, claims())

    assert rejection(validator, bearer(token)).reason is Reason.MISSING_KID
    assert keys.looked_up == []


# Check 3: `kid` matches a key in the provider's JWKS.


def test_3_an_unknown_kid_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(), kid="us-east-1_foreignpool")

    failure = rejection(validator, bearer(token))

    assert failure.reason is Reason.UNKNOWN_KID
    assert failure.kid == "us-east-1_foreignpool"


def test_3_no_keys_at_all_is_not_a_rejection_but_an_outage(private_key: rsa.RSAPrivateKey) -> None:
    validator = TokenValidator(cognito_config(), NoKeys())

    with pytest.raises(SigningKeysUnavailable):
        validator.validate(bearer(signed(private_key, claims())))


# Check 4: the signature is valid.


def test_4_real_claims_signed_with_another_key_are_rejected(
    validator: TokenValidator, other_key: rsa.RSAPrivateKey
) -> None:
    assert rejection(validator, bearer(signed(other_key, claims()))).reason is Reason.BAD_SIGNATURE


def test_4_a_tampered_payload_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    header, _, signature = signed(private_key, claims()).split(".")
    forged = b64(json.dumps(claims(sub="mallory")).encode())

    failure = rejection(validator, bearer(f"{header}.{forged}.{signature}"))

    assert failure.reason is Reason.BAD_SIGNATURE


def test_4_is_checked_before_the_claims(
    validator: TokenValidator, other_key: rsa.RSAPrivateKey
) -> None:
    token = signed(other_key, claims(iss="http://evil.example/pool", exp=1, token_use="id"))

    assert rejection(validator, bearer(token)).reason is Reason.BAD_SIGNATURE


# Check 5: `iss` equals the configured issuer exactly.


@pytest.mark.parametrize(
    "issuer", ["http://evil.example/pool", f"{ISSUER}/", ISSUER.upper(), None, 42]
)
def test_5_a_wrong_or_missing_issuer_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, issuer: Any
) -> None:
    token = signed(private_key, claims(iss=issuer))

    assert rejection(validator, bearer(token)).reason is Reason.WRONG_ISSUER


def test_5_is_checked_before_the_time(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(iss="http://evil.example/pool", exp=1))

    assert rejection(validator, bearer(token)).reason is Reason.WRONG_ISSUER


# Check 6: `exp` in the future; `iat` and `nbf` not in the future; 30 seconds of tolerance.


def test_6_an_expired_token_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    now = int(time.time())
    token = signed(private_key, claims(iat=now - 960, exp=now - LEEWAY - 5))

    assert rejection(validator, bearer(token)).reason is Reason.EXPIRED


def test_6_a_token_expired_within_the_tolerance_is_accepted(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    now = int(time.time())
    token = signed(private_key, claims(iat=now - 900, exp=now - LEEWAY + 5))

    assert validator.validate(bearer(token)).subject == "alice-sub"


@pytest.mark.parametrize("claim", ["iat", "nbf"])
def test_6_a_token_from_the_future_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, claim: str
) -> None:
    token = signed(private_key, claims(**{claim: int(time.time()) + LEEWAY + 5}))

    assert rejection(validator, bearer(token)).reason is Reason.NOT_YET_VALID


@pytest.mark.parametrize("claim", ["iat", "nbf"])
def test_6_clock_skew_within_the_tolerance_is_accepted(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, claim: str
) -> None:
    token = signed(private_key, claims(**{claim: int(time.time()) + LEEWAY - 5}))

    assert validator.validate(bearer(token)).subject == "alice-sub"


def test_6_iat_and_nbf_are_optional(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(iat=None))

    assert validator.validate(bearer(token)).subject == "alice-sub"


@pytest.mark.parametrize(
    "overrides", [{"exp": None}, {"exp": "tomorrow"}, {"iat": "now"}, {"nbf": "later"}]
)
def test_6_missing_or_unreadable_time_claims_are_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, overrides: dict[str, Any]
) -> None:
    token = signed(private_key, claims(**overrides))

    assert rejection(validator, bearer(token)).reason is Reason.INVALID_TIME


def test_6_is_checked_before_the_token_type(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(exp=1, token_use="id"))

    assert rejection(validator, bearer(token)).reason is Reason.EXPIRED


# Check 7: it is an access token, by the configured marker claim.


@pytest.mark.parametrize("token_use", ["id", "refresh", "", None])
def test_7_anything_but_an_access_token_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, token_use: str | None
) -> None:
    token = signed(private_key, claims(token_use=token_use))

    assert rejection(validator, bearer(token)).reason is Reason.NOT_AN_ACCESS_TOKEN


def test_7_the_marker_claim_is_configurable(
    keys: FakeKeySource, private_key: rsa.RSAPrivateKey
) -> None:
    keycloak = TokenValidator(
        cognito_config(token_type_claim="typ", token_type_value="Bearer"), keys
    )

    assert keycloak.validate(bearer(signed(private_key, claims(typ="Bearer")))).subject
    failure = rejection(keycloak, bearer(signed(private_key, claims(typ="ID"))))
    assert failure.reason is Reason.NOT_AN_ACCESS_TOKEN


def test_7_is_checked_before_the_client(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(token_use="id", client_id=None, aud=CLIENT))

    assert rejection(validator, bearer(token)).reason is Reason.NOT_AN_ACCESS_TOKEN


# Check 8: the client claim is on the allow-list.


@pytest.mark.parametrize("client_id", ["orders-api-untrusted-client", "", None, ["x"], 42])
def test_8_a_client_not_on_the_allow_list_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, client_id: Any
) -> None:
    token = signed(private_key, claims(client_id=client_id))

    failure = rejection(validator, bearer(token))

    assert failure.reason is Reason.CLIENT_NOT_ALLOWED


def test_8_the_client_claim_is_configurable_and_may_be_a_list(
    keys: FakeKeySource, private_key: rsa.RSAPrivateKey
) -> None:
    entra = TokenValidator(cognito_config(client_claim="aud"), keys)

    assert entra.validate(bearer(signed(private_key, claims(aud=["other", CLIENT])))).subject
    failure = rejection(entra, bearer(signed(private_key, claims(aud=["other"]))))
    assert failure.reason is Reason.CLIENT_NOT_ALLOWED


def test_8_is_checked_before_the_subject(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(client_id="someone-else", sub=None))

    assert rejection(validator, bearer(token)).reason is Reason.CLIENT_NOT_ALLOWED


# Check 9: `sub` is present.


@pytest.mark.parametrize("sub", [None, "", 42])
def test_9_a_missing_subject_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey, sub: Any
) -> None:
    token = signed(private_key, claims(sub=sub))

    assert rejection(validator, bearer(token)).reason is Reason.MISSING_SUBJECT


# Roles: read from the configured claim, mapped through the configured groups.


def test_a_token_without_groups_has_no_roles(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(**{"cognito:groups": None}))

    assert validator.validate(bearer(token)).roles == ()


def test_groups_are_mapped_to_roles_and_unmapped_groups_ignored(
    keys: FakeKeySource, private_key: rsa.RSAPrivateKey
) -> None:
    mapped = TokenValidator(
        cognito_config(group_roles={"shop-staff": "ORDER_MANAGER", "customers": "USER"}), keys
    )
    token = signed(private_key, claims(**{"cognito:groups": ["customers", "shop-staff", "x"]}))

    assert mapped.validate(bearer(token)).roles == ("USER", "ORDER_MANAGER")


def test_a_nested_roles_claim_is_found_by_path(
    keys: FakeKeySource, private_key: rsa.RSAPrivateKey
) -> None:
    keycloak = TokenValidator(cognito_config(roles_claim="realm_access.roles"), keys)
    token = signed(private_key, claims(realm_access={"roles": ["ADMIN"]}))

    assert keycloak.validate(bearer(token)).roles == ("ADMIN",)


def test_an_unreadable_roles_claim_means_no_roles(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(**{"cognito:groups": {"not": "a list"}}))

    assert validator.validate(bearer(token)).roles == ()


# What a rejection tells the logs: the reason plus kid, iss and client_id, never the token.


def test_a_rejection_carries_kid_issuer_and_client_for_the_logs(
    validator: TokenValidator, other_key: rsa.RSAPrivateKey
) -> None:
    token = signed(other_key, claims(client_id="forged-client"))

    failure = rejection(validator, bearer(token))

    assert failure.kid == KID
    assert failure.issuer == ISSUER
    assert failure.client_id == "forged-client"
    assert token not in str(failure)


def test_logged_token_details_are_truncated(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = signed(private_key, claims(iss="x" * 5000), kid="k" * 5000)

    failure = rejection(validator, bearer(token))

    assert failure.kid is not None
    assert failure.issuer is not None
    assert len(failure.kid) <= 128
    assert len(failure.issuer) <= 128


def test_only_rs256_is_allowed_by_default() -> None:
    assert cognito_config().algorithms == ("RS256",)
    assert cognito_config().leeway_seconds == LEEWAY
