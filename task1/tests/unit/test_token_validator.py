import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from orders_api.auth.tokens import InvalidToken, TokenValidator, ValidatorConfig

ISSUER = "http://floci:4566/us-east-1_orderspool"
KID = "us-east-1_orderspool"


class FakeKeySource:
    def __init__(self, keys: dict[str, Any]) -> None:
        self.keys = keys

    def signing_key(self, kid: str) -> Any:
        return self.keys.get(kid)


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def validator(private_key: rsa.RSAPrivateKey) -> TokenValidator:
    return TokenValidator(
        ValidatorConfig(issuer=ISSUER), FakeKeySource({KID: private_key.public_key()})
    )


def access_token(key: rsa.RSAPrivateKey, **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": "alice-sub",
        "iss": ISSUER,
        "iat": now,
        "exp": now + 900,
        "token_use": "access",
        "client_id": "orders-api-client",
        "cognito:groups": ["USER"],
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": KID})


def test_valid_token_yields_subject_and_groups(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    identity = validator.validate(access_token(private_key, **{"cognito:groups": ["USER", "X"]}))

    assert identity.subject == "alice-sub"
    assert identity.groups == ("USER", "X")


def test_token_without_groups_has_no_groups(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    token = access_token(private_key)
    claims = jwt.decode(token, options={"verify_signature": False})
    del claims["cognito:groups"]

    identity = validator.validate(jwt.encode(claims, private_key, "RS256", {"kid": KID}))

    assert identity.groups == ()


def test_signature_from_another_key_is_rejected(validator: TokenValidator) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(InvalidToken, match="signature"):
        validator.validate(access_token(other))


def test_wrong_issuer_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    with pytest.raises(InvalidToken, match="issuer"):
        validator.validate(access_token(private_key, iss="http://evil.example/pool"))


def test_expired_token_is_rejected(
    validator: TokenValidator, private_key: rsa.RSAPrivateKey
) -> None:
    past = int(time.time()) - 3600
    with pytest.raises(InvalidToken, match="expired"):
        validator.validate(access_token(private_key, iat=past - 900, exp=past))


def test_unknown_key_id_is_rejected(private_key: rsa.RSAPrivateKey) -> None:
    validator = TokenValidator(ValidatorConfig(issuer=ISSUER), FakeKeySource({}))

    with pytest.raises(InvalidToken, match="key"):
        validator.validate(access_token(private_key))


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c"])
def test_malformed_token_is_rejected(validator: TokenValidator, garbage: str) -> None:
    with pytest.raises(InvalidToken, match="malformed"):
        validator.validate(garbage)
