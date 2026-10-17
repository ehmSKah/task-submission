"""`make demo`: the reviewer's one-minute scenario against the deployed stack.

The scenario table and the runner are tested here without a stack: the table promises the
statuses the README shows, and the runner stops at the first unexpected one, never prints a
token, and always gives the previous rate limits back.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from orders_api.domain.access import Role
from orders_api_infra.demo import (
    ALG_NONE,
    NO_TOKEN,
    RATE_LIMIT_HEADERS,
    SCENARIO,
    Call,
    Step,
    Target,
    alg_none_token,
    run,
    unmet,
)
from orders_api_infra.demo_users import DEMO_USERS
from orders_api_infra.rate_limits import PRESETS

ORDER_IDS = iter(f"0e6bb2c6-0000-4000-8000-{n:012d}" for n in range(1, 1000))
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def a_real_token(user: str = "alice") -> str:
    """Shaped like a Cognito access token: RS256 with a `kid`, a `jti` and the usual claims."""
    claims = {"sub": f"sub-of-{user}", "token_use": "access", "client_id": "c", "jti": "j-1"}
    return jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": "pool-key"})


def calls(scenario: tuple[Step, ...] = SCENARIO) -> list[Call]:
    return [call for step in scenario for call in step.calls]


# The scenario table.


def test_the_scenario_shows_the_readmes_statuses_in_order() -> None:
    tiny_user_limit = PRESETS["tiny"].per_role[Role.USER]

    assert [call.expected for call in calls()] == [
        200,  # health, no token
        201,  # alice creates an order to cancel
        201,  # ... and one to ship
        404,  # bob can't see alice's order
        200,  # alice cancels her order
        200,  # the manager confirms the other
        200,  # ... and ships it
        409,  # alice can't cancel a shipped order
        403,  # alice can't delete
        204,  # the admin can
        400,  # the gateway rejects a body with an unknown field
        401,  # alg: none
        *[200] * tiny_user_limit,  # within the tiny USER limit
        429,  # over it
    ]


def test_the_rate_limit_step_sends_one_request_more_than_a_users_tiny_limit() -> None:
    [step] = [step for step in SCENARIO if step.preset is not None]
    limit = PRESETS[step.preset].per_role[Role.USER]

    assert step.preset == "tiny"
    assert len(step.calls) == limit + 1
    assert {call.caller for call in step.calls} == {"alice"}  # a USER
    assert [call.expected for call in step.calls] == [200] * limit + [429]


def test_every_order_a_path_names_was_created_by_an_earlier_call() -> None:
    created: set[str] = set()
    for call in calls():
        assert set(re.findall(r"{(\w+)}", call.path)) <= created, call
        if call.save_as is not None:
            created.add(call.save_as)


def test_every_caller_is_a_demo_user_no_token_or_the_forged_token() -> None:
    allowed = {user.name for user in DEMO_USERS} | {NO_TOKEN, ALG_NONE}

    assert {call.caller for call in calls()} <= allowed


def test_the_gateway_step_sends_an_unknown_field() -> None:
    [rejected] = [call for call in calls() if call.expected == 400]

    assert rejected.body is not None
    assert "totalAmount" in rejected.body  # computed by the API, never accepted (ADR 0004)
    assert rejected.from_gateway


def test_a_created_order_shows_its_etag_and_the_rate_limit_headers() -> None:
    created = [call for call in calls() if call.expected == 201]

    assert created
    for call in created:
        assert set(call.headers) == {
            "ETag",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
        }


def test_the_refused_request_says_when_to_retry() -> None:
    [refused] = [call for call in calls() if call.expected == 429]

    assert refused.headers == ("Retry-After",)


def test_steps_are_numbered_in_order() -> None:
    assert [step.title.split(".")[0] for step in SCENARIO] == [
        str(n) for n in range(1, len(SCENARIO) + 1)
    ]


# The forged token.


def test_an_alg_none_token_keeps_the_real_header_and_claims_without_a_signature() -> None:
    real = a_real_token()

    forged = alg_none_token(real)

    *_, signature = forged.split(".")
    header = jwt.get_unverified_header(forged)
    claims = jwt.decode(forged, options={"verify_signature": False})
    assert signature == ""
    assert header == {**jwt.get_unverified_header(real), "alg": "none"}
    assert header["kid"] == "pool-key"
    real_claims = jwt.decode(real, options={"verify_signature": False})
    assert {**claims, "jti": real_claims["jti"]} == real_claims


def test_every_forged_token_is_new_to_the_authorizers_cache() -> None:
    real = a_real_token()

    assert alg_none_token(real) != alg_none_token(real)


def test_a_forged_token_fails_signature_verification() -> None:
    with pytest.raises(jwt.InvalidTokenError):
        jwt.decode(alg_none_token(a_real_token()), SIGNING_KEY.public_key(), algorithms=["RS256"])


# What a response must show besides its status.

FUNCTION_HEADERS = {"RateLimit-Limit": "3", "RateLimit-Remaining": "2", "RateLimit-Reset": "41"}
VALIDATION = {"type": "urn:problem:orders-api:validation-error", "status": 400}


def test_a_response_with_the_expected_status_and_headers_meets_the_call() -> None:
    call = Call("alice", "POST", "/api/v1/orders", 201, headers=("ETag", *RATE_LIMIT_HEADERS))
    response = httpx.Response(201, headers={**FUNCTION_HEADERS, "ETag": '"1"'})

    assert unmet(call, response) == []


def test_a_wrong_status_is_reported_with_the_expected_one() -> None:
    call = Call("bob", "GET", "/api/v1/orders/x", 404)

    assert unmet(call, httpx.Response(200)) == ["expected 404"]


def test_each_missing_header_is_reported() -> None:
    call = Call("alice", "POST", "/api/v1/orders", 201, headers=("ETag", *RATE_LIMIT_HEADERS))
    response = httpx.Response(201, headers={"RateLimit-Limit": "3"})

    assert unmet(call, response) == [
        "no ETag header",
        "no RateLimit-Remaining header",
        "no RateLimit-Reset header",
    ]


def test_the_gateways_own_400_meets_a_gateway_call() -> None:
    """The validator's message in `detail`, no `errors[]`, and none of the function's headers."""
    call = Call("alice", "POST", "/api/v1/orders", 400, from_gateway=True)
    body = {**VALIDATION, "detail": "[object has extra properties: totalAmount]"}

    assert unmet(call, httpx.Response(400, json=body)) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json=VALIDATION, headers=FUNCTION_HEADERS),
        httpx.Response(400, json={**VALIDATION, "errors": [{"pointer": "/x", "detail": "no"}]}),
    ],
    ids=["rate-limit-headers", "errors"],
)
def test_a_400_from_the_function_fails_a_gateway_call(response: httpx.Response) -> None:
    call = Call("alice", "POST", "/api/v1/orders", 400, from_gateway=True)

    assert unmet(call, response) == ["answered by the function, not the gateway"]


# The runner.


class FakeApi:
    """Answers each request with the next status of a script (or fails to connect where the
    script says `None`), and records what it was sent and which orders it created."""

    def __init__(self, statuses: list[int | None]) -> None:
        self.statuses = statuses
        self.requests: list[httpx.Request] = []
        self.created: list[str] = []
        self.without_header: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self.answer(request)
        if self.without_header is not None:
            response.headers.pop(self.without_header, None)
        return response

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self.statuses.pop(0)
        if status is None:
            raise httpx.ConnectError("connection refused", request=request)
        headers = dict(FUNCTION_HEADERS)
        if status == 201:
            self.created.append(next(ORDER_IDS))
            return httpx.Response(
                201, json={"id": self.created[-1]}, headers={**headers, "ETag": '"1"'}
            )
        if status == 429:
            headers["Retry-After"] = "42"
        if status >= 400:
            problem = {"type": f"urn:problem:orders-api:status-{status}", "status": status}
            if status == 429:
                problem["scope"] = "user"
            return httpx.Response(status, json=problem, headers=headers)
        return httpx.Response(status, headers=headers)


@pytest.fixture
def presets() -> list[str]:
    """Records the preset switches: `+tiny` when one comes into force, `-tiny` when it goes."""
    return []


@pytest.fixture
def target(presets: list[str]) -> Callable[[FakeApi], Target]:
    @contextmanager
    def preset(name: str) -> Iterator[None]:
        presets.append(f"+{name}")
        try:
            yield
        finally:
            presets.append(f"-{name}")

    def make(api: FakeApi) -> Target:
        return Target(
            client=httpx.Client(transport=httpx.MockTransport(api), base_url="http://api.test"),
            token_for=a_real_token,
            preset=preset,
            reset_counters=lambda: presets.append("reset"),
        )

    return make


SMALL = (
    Step("1. Public", (Call(NO_TOKEN, "GET", "/api/v1/health", 200),)),
    Step(
        "2. Own orders",
        (
            Call(
                "alice",
                "POST",
                "/api/v1/orders",
                201,
                body={"currency": "EUR"},
                save_as="a",
                headers=("ETag", *RATE_LIMIT_HEADERS),
            ),
            Call("bob", "GET", "/api/v1/orders/{a}", 404),
            Call(ALG_NONE, "GET", "/api/v1/orders/{a}", 401),
        ),
    ),
    Step(
        "3. Limits",
        (
            Call("alice", "GET", "/api/v1/orders", 200),
            Call("alice", "GET", "/api/v1/orders", 429, headers=("Retry-After",)),
        ),
        preset="tiny",
    ),
)


def test_each_request_is_printed_with_its_status_and_the_run_succeeds(
    target: Callable[[FakeApi], Target], presets: list[str]
) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 429])
    lines: list[str] = []

    assert run(SMALL, target(api), out=lines.append) is True

    output = "\n".join(lines)
    assert [line for line in lines if line.startswith(("1.", "2.", "3."))] == [
        "1. Public",
        "2. Own orders",
        "3. Limits",
    ]
    assert output.count("✓") == 6
    assert "✗" not in output
    assert re.search(r"✓ 201 +POST +/api/v1/orders +alice +ETag \"1\"", output)
    assert re.search(r"✓ 429 .*Retry-After 42", output)
    assert "scope user" in output
    assert presets == ["reset", "+tiny", "-tiny"]


def test_a_created_orders_id_goes_into_later_paths(target: Callable[[FakeApi], Target]) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 429])

    run(SMALL, target(api), out=lambda _: None)

    [order_id] = api.created
    assert json.loads(api.requests[1].content) == {"currency": "EUR"}
    assert api.requests[2].url.path == f"/api/v1/orders/{order_id}"
    assert api.requests[3].url.path == f"/api/v1/orders/{order_id}"


def test_each_caller_sends_their_own_token_and_no_token_sends_none(
    target: Callable[[FakeApi], Target],
) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 429])

    run(SMALL, target(api), out=lambda _: None)

    authorizations = [request.headers.get("Authorization") for request in api.requests]
    assert authorizations[0] is None
    assert authorizations[1] == f"Bearer {a_real_token('alice')}"
    assert authorizations[2] == f"Bearer {a_real_token('bob')}"
    forged = authorizations[3]
    assert forged is not None
    assert jwt.get_unverified_header(forged.removeprefix("Bearer "))["alg"] == "none"


def test_no_token_is_ever_printed(target: Callable[[FakeApi], Target]) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 429])
    lines: list[str] = []

    run(SMALL, target(api), out=lines.append)

    assert api.requests[1].headers["Authorization"].startswith("Bearer eyJ")
    assert "eyJ" not in "\n".join(lines)  # what every JWT starts with


def test_an_unexpected_status_stops_the_run_and_fails_it(
    target: Callable[[FakeApi], Target],
) -> None:
    api = FakeApi([200, 201, 200])  # bob sees alice's order
    lines: list[str] = []

    assert run(SMALL, target(api), out=lines.append) is False

    output = "\n".join(lines)
    assert re.search(r"✗ 200 +GET .*expected 404", output)
    assert len(api.requests) == 3  # nothing after the failure
    assert "3. Limits" not in output


def test_a_missing_header_stops_the_run_and_fails_it(
    target: Callable[[FakeApi], Target],
) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 429])
    api.without_header = "Retry-After"
    lines: list[str] = []

    assert run(SMALL, target(api), out=lines.append) is False

    assert re.search(r"✗ 429 +GET .*no Retry-After header", "\n".join(lines))


def test_the_previous_limits_come_back_when_a_rate_limited_call_fails(
    target: Callable[[FakeApi], Target], presets: list[str]
) -> None:
    api = FakeApi([200, 201, 404, 401, 200, 200])  # never refused

    assert run(SMALL, target(api), out=lambda _: None) is False

    assert presets == ["reset", "+tiny", "-tiny"]


def test_the_previous_limits_come_back_when_the_api_cannot_be_reached(
    presets: list[str], target: Callable[[FakeApi], Target]
) -> None:
    api = FakeApi([200, 201, 404, 401, None])

    with pytest.raises(httpx.ConnectError):
        run(SMALL, target(api), out=lambda _: None)

    assert presets == ["reset", "+tiny", "-tiny"]
