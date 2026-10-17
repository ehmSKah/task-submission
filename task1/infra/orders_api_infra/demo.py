"""`make demo`: a one-minute scenario against the deployed stack, for a reviewer.

Each request is printed with its status. The run stops and fails at the first status that isn't
the one the design promises. Tokens are real Cognito tokens of the demo users (ADR 0010), plus
one forged `alg: none` token; none is ever printed. The rate-limit step switches the runtime
limits (ADR 0006) and restores the previous ones afterwards, also when it fails.

Usage: python -m orders_api_infra.demo --env local
"""

import argparse
import base64
import json
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from functools import cache
from typing import Any

import httpx
import jwt

from orders_api.domain.access import Role
from orders_api_infra import rate_limits
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.demo_users import access_token, demo_user
from orders_api_infra.paths import BUILD_DIR
from orders_api_infra.rate_limits import PRESETS

ALG_NONE = "alg-none"
"""The caller who sends alice's real claims unsigned, with `alg: none`."""

ORDER: Mapping[str, Any] = {
    "currency": "EUR",
    "items": [
        {"sku": "ABC-123", "quantity": 2, "unitPrice": 1999},
        {"sku": "XYZ-9", "quantity": 3, "unitPrice": 250},
    ],
}
PROBLEM_TYPE_PREFIX = "urn:problem:orders-api:"
WINDOW_SECONDS_NEEDED = 10
"""Of the current rate-limit window, so the rate-limit step's requests all fall into one."""


@dataclass(frozen=True)
class Call:
    """One request and the status the design promises for it."""

    caller: str | None  # a demo user, ALG_NONE, or None for no token
    method: str
    path: str  # `{name}` stands for the id of the order an earlier call saved as `name`
    expected: int
    body: Mapping[str, Any] | None = None
    save_as: str | None = None


@dataclass(frozen=True)
class Step:
    title: str
    calls: tuple[Call, ...]
    preset: str | None = None
    """A rate-limit preset in force for this step's calls, starting from empty counters."""


def _status(order: str, status: str, caller: str, expected: int) -> Call:
    return Call(caller, "PATCH", f"/api/v1/orders/{{{order}}}/status", expected, {"status": status})


TINY_USER_LIMIT = PRESETS["tiny"].per_role[Role.USER]

SCENARIO: tuple[Step, ...] = (
    Step("1. Health is public: no token", (Call(None, "GET", "/api/v1/health", 200),)),
    Step(
        "2. alice (USER) creates two orders",
        (
            Call("alice", "POST", "/api/v1/orders", 201, ORDER, save_as="a"),
            Call("alice", "POST", "/api/v1/orders", 201, ORDER, save_as="b"),
        ),
    ),
    Step(
        "3. bob (USER) asks for alice's order: 404, so its existence doesn't leak",
        (Call("bob", "GET", "/api/v1/orders/{a}", 404),),
    ),
    Step(
        "4. alice cancels one order; the manager confirms and ships the other; then alice can't "
        "cancel it (409)",
        (
            _status("a", "CANCELLED", "alice", 200),
            _status("b", "CONFIRMED", "manager", 200),
            _status("b", "SHIPPED", "manager", 200),
            _status("b", "CANCELLED", "alice", 409),
        ),
    ),
    Step(
        "5. Deleting is for ADMIN only: alice gets 403, the admin 204",
        (
            Call("alice", "DELETE", "/api/v1/orders/{b}", 403),
            Call("admin", "DELETE", "/api/v1/orders/{b}", 204),
        ),
    ),
    Step(
        "6. The gateway rejects a body with an unknown field (totalAmount) before the function",
        (Call("alice", "POST", "/api/v1/orders", 400, {**ORDER, "totalAmount": 1}),),
    ),
    Step(
        "7. A forged token with alg: none: 401 from the authorizer",
        (Call(ALG_NONE, "GET", "/api/v1/orders/{a}", 401),),
    ),
    Step(
        f"8. Runtime limits (preset tiny, no deploy): alice is refused after {TINY_USER_LIMIT} "
        "requests; the previous limits come back afterwards",
        (
            *[Call("alice", "GET", "/api/v1/orders", 200) for _ in range(TINY_USER_LIMIT)],
            Call("alice", "GET", "/api/v1/orders", 429),
        ),
        preset="tiny",
    ),
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def alg_none_token(real: str) -> str:
    """A real token's header and claims with `alg: none` and no signature. The `jti` is new each
    time, so the authorizer's per-token cache can't answer for it (ADR 0003)."""
    header = {**jwt.get_unverified_header(real), "alg": "none"}
    claims = {**jwt.decode(real, options={"verify_signature": False}), "jti": str(uuid.uuid4())}
    return f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}."


@dataclass(frozen=True)
class Target:
    """The deployed stack, and what the runner needs from it."""

    client: httpx.Client
    token_for: Callable[[str], str]
    preset: Callable[[str], AbstractContextManager[object]]
    """Puts a preset in force with empty counters, and the previous limits back afterwards."""
    reset_counters: Callable[[], None]
    """Empties the counters, so the demo's counts are its own."""


def _headers(caller: str | None, target: Target) -> dict[str, str]:
    if caller is None:
        return {}
    if caller == ALG_NONE:
        token = alg_none_token(target.token_for("alice"))
    else:
        token = target.token_for(caller)
    return {"Authorization": f"Bearer {token}"}


def _details(response: httpx.Response) -> str:
    """What the response says beyond its status: never a header that could carry a token."""
    details = []
    if "ETag" in response.headers:
        details.append(f"ETag {response.headers['ETag']}")
    if response.status_code >= 400 and response.content:
        problem = response.json()
        kind = str(problem.get("type", "")).removeprefix(PROBLEM_TYPE_PREFIX)
        details.append(kind + (f" (scope {problem['scope']})" if "scope" in problem else ""))
    for header in ("Retry-After", "RateLimit-Remaining"):
        if header in response.headers:
            details.append(f"{header} {response.headers[header]}")
    return ", ".join(details)


def _send(call: Call, saved: dict[str, str], target: Target, out: Callable[[str], None]) -> bool:
    path = call.path.format(**saved)
    shown = call.path.format(**{name: f"{order_id[:8]}…" for name, order_id in saved.items()})
    response = target.client.request(
        call.method,
        path,
        headers=_headers(call.caller, target),
        json=None if call.body is None else dict(call.body),
    )
    ok = response.status_code == call.expected
    mark, caller = "✓" if ok else "✗", call.caller or "no token"
    line = f"   {mark} {response.status_code} {call.method:<6} {shown:<38} {caller:<8}"
    out(f"{line} {_details(response) if ok else f'expected {call.expected}'}".rstrip())
    if ok and call.save_as is not None:
        saved[call.save_as] = str(response.json()["id"])
    return ok


def run(scenario: Sequence[Step], target: Target, out: Callable[[str], None] = print) -> bool:
    """Send every step's calls in order; False at the first unexpected status."""
    target.reset_counters()
    saved: dict[str, str] = {}
    for step in scenario:
        out(step.title)
        with target.preset(step.preset) if step.preset else _nothing():
            for call in step.calls:
                if not _send(call, saved, target, out):
                    return False
    return True


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def _target(config: EnvConfig, client: httpx.Client) -> Target:
    @cache
    def token_for(user: str) -> str:
        return access_token(config, demo_user(user))

    @contextmanager
    def preset(name: str) -> Iterator[None]:
        with rate_limits.preset_in_force(config, name):
            rate_limits.wait_for_window(WINDOW_SECONDS_NEEDED)
            rate_limits.empty_counters(config)
            yield

    def reset_counters() -> None:
        rate_limits.empty_counters(config)

    return Target(client=client, token_for=token_for, preset=preset, reset_counters=reset_counters)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    args = parser.parse_args()
    config = load_config(args.env)
    endpoints_file = BUILD_DIR / "endpoints.json"
    if not endpoints_file.is_file():
        raise SystemExit("build/endpoints.json is missing: run `make up` first")
    api_url = json.loads(endpoints_file.read_text())["apiUrl"]
    print(f"Orders API demo against {api_url} (env {config.name})\n")
    with httpx.Client(base_url=api_url, timeout=30) as client:
        passed = run(SCENARIO, _target(config, client))
    print("\nEvery status was the expected one." if passed else "\nThe demo failed.")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
