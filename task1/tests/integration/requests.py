"""Request builders shared by the integration tests."""

from typing import Any

import jwt

NEW_ORDER: dict[str, Any] = {
    "currency": "EUR",
    "items": [
        {"sku": "ABC-123", "quantity": 2, "unitPrice": 1999},
        {"sku": "XYZ-9", "quantity": 3, "unitPrice": 250},
    ],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def subject_of(token: str) -> str:
    """The token's `sub`; the authorizer has verified it, the test only reads it."""
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    return str(claims["sub"])
