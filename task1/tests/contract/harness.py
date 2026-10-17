"""The pure parts of the contract run: its configuration, the orders it can find, the caller's
token and a count of what came back (ADR 0011). The deployment-facing parts are in conftest.py.
"""

import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from schemathesis import AuthContext, Case
from schemathesis.config import SchemathesisConfig

from orders_api.domain.order import Order, OrderStatus
from tests.builders import an_order

CONFIG = Path(__file__).with_name("schemathesis.toml")

KNOWN_ORDERS = "known_orders"
"""The Schemathesis dictionary `path.id` draws from (see schemathesis.toml)."""
DELETABLE_ORDERS = "deletable_orders"
"""The one `DELETE` draws from instead, so deletes never remove the orders the other operations
are meant to find, whichever order Schemathesis runs the operations in."""

# Fixed ids, so every run starts from the same orders: each run writes them afresh.
KNOWN_ORDER_IDS = tuple(f"c0de0000-0000-4000-8000-00000000000{n}" for n in range(1, 7))
DELETABLE_ORDER_IDS = tuple(f"c0de0000-0000-4000-8000-0000000000d{n}" for n in range(1, 3))


def known_orders(*, owner: str, other: str) -> list[Order]:
    """Orders for the generated requests to reach. Known: one of `owner`'s in each status and one
    of `other`'s, for reads and every status change with its conflicts (or, for a `USER`, the
    `403`s and someone else's order as a `404`). Deletable: one of each owner's."""
    owned = [
        an_order(id=order_id, owner_id=owner, status=status)
        for order_id, status in zip(KNOWN_ORDER_IDS, OrderStatus, strict=False)
    ]
    others = an_order(id=KNOWN_ORDER_IDS[len(owned)], owner_id=other)
    deletable = [
        an_order(id=order_id, owner_id=owner_id)
        for order_id, owner_id in zip(DELETABLE_ORDER_IDS, (owner, other), strict=True)
    ]
    return [*owned, others, *deletable]


def config_with_known_orders(config_text: str) -> SchemathesisConfig:
    """The run's settings, with the known and deletable order ids in their dictionaries."""
    data = tomllib.loads(config_text)
    data["dictionaries"][KNOWN_ORDERS]["values"] = list(KNOWN_ORDER_IDS)
    data["dictionaries"][DELETABLE_ORDERS]["values"] = list(DELETABLE_ORDER_IDS)
    return SchemathesisConfig.from_dict(data)


def bearer_token(token: Callable[[], str]) -> type:
    """A Schemathesis auth provider sending `token()` as the caller's bearer token. Schemathesis
    leaves it out where it tests a missing or invalid token itself."""

    class BearerToken:
        def get(self, case: Case, context: AuthContext) -> str:
            return token()

        def set(self, case: Case, data: str, context: AuthContext) -> None:
            case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}

    return BearerToken


def success_statuses(spec: Mapping[str, Any]) -> dict[str, int]:
    """Each operation's one success status, by `METHOD path`."""
    return {
        f"{method.upper()} {path}": next(
            int(status) for status in operation["responses"] if status.startswith("2")
        )
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and "responses" in operation
    }


class StatusesSeen:
    """How often each caller got each status from each operation, for the run's summary."""

    def __init__(self) -> None:
        self._counts: defaultdict[tuple[str, str], Counter[int]] = defaultdict(Counter)

    def record(self, caller: str, operation: str, status: int) -> None:
        self._counts[caller, operation][status] += 1

    def lines(self) -> list[str]:
        return [
            f"{caller}  {operation}: "
            + ", ".join(f"{status} x{count}" for status, count in sorted(counts.items()))
            for (caller, operation), counts in sorted(self._counts.items())
        ]

    def never_succeeded(self, expected: Mapping[str, int]) -> dict[str, int]:
        """The operations whose success status no caller ever got."""
        return {
            operation: status
            for operation, status in expected.items()
            if not any(
                counts[status]
                for (_, seen_operation), counts in self._counts.items()
                if seen_operation == operation
            )
        }
