"""The signing-key cache of ADR 0003: discovery, 1 h TTL, rate-limited refetch, stale keys."""

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from orders_api.auth.oidc import (
    FETCH_TIMEOUT_SECONDS,
    JwksCache,
    KeyFetchError,
    SigningKeysUnavailable,
    fetch_json,
)

ISSUER = "http://floci:4566/us-east-1_orderspool"
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"


def jwk(kid: str, **overrides: Any) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key()
    public: dict[str, Any] = RSAAlgorithm.to_jwk(key, as_dict=True)
    return {**public, "kid": kid, "alg": "RS256", "use": "sig", **overrides}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    """Discovery and JWKS documents by URL; can go offline."""

    def __init__(self, *kids: str) -> None:
        self.documents: dict[str, dict[str, Any]] = {
            DISCOVERY: {"issuer": ISSUER, "jwks_uri": JWKS_URI},
            JWKS_URI: {"keys": [jwk(kid) for kid in kids]},
        }
        self.requested: list[str] = []
        self.offline = False

    def publish(self, *kids: str) -> None:
        self.documents[JWKS_URI] = {"keys": [jwk(kid) for kid in kids]}

    def __call__(self, url: str) -> dict[str, Any]:
        self.requested.append(url)
        if self.offline:
            raise KeyFetchError(f"{url}: timed out")
        if url not in self.documents:
            raise KeyFetchError(f"{url}: 404")
        return self.documents[url]

    def jwks_fetches(self) -> int:
        return self.requested.count(JWKS_URI)


class RecordedEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def __call__(self, event: str, /, **fields: object) -> None:
        self.events.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def events() -> RecordedEvents:
    return RecordedEvents()


def cache_for(
    provider: FakeProvider, clock: FakeClock, events: RecordedEvents, **options: Any
) -> JwksCache:
    return JwksCache(ISSUER, fetch_json=provider, clock=clock, events=events, **options)


def test_keys_are_found_through_oidc_discovery(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("pool-key")

    cache = cache_for(provider, clock, events)

    assert cache.signing_key("pool-key") is not None
    assert provider.requested == [DISCOVERY, JWKS_URI]
    assert events.names() == ["jwks_fetched"]


def test_a_configured_jwks_uri_skips_discovery(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("pool-key")

    cache = cache_for(provider, clock, events, jwks_uri=JWKS_URI)

    assert cache.signing_key("pool-key") is not None
    assert provider.requested == [JWKS_URI]


def test_keys_are_reused_within_the_ttl(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("pool-key")
    cache = cache_for(provider, clock, events)

    cache.signing_key("pool-key")
    clock.advance(3599)
    cache.signing_key("pool-key")

    assert provider.jwks_fetches() == 1


def test_keys_are_refreshed_after_one_hour(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("old-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("old-key")

    provider.publish("new-key")
    clock.advance(3600)

    assert cache.signing_key("new-key") is not None
    assert cache.signing_key("old-key") is None
    assert provider.jwks_fetches() == 2
    # Discovery expires with the keys, so a moved JWKS location is picked up too.
    assert provider.requested.count(DISCOVERY) == 2


def test_a_moved_jwks_location_is_found_at_the_next_refresh(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("old-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("old-key")

    moved = f"{ISSUER}/keys-v2"
    provider.documents[DISCOVERY] = {"issuer": ISSUER, "jwks_uri": moved}
    provider.documents[moved] = {"keys": [jwk("new-key")]}
    clock.advance(3600)

    assert cache.signing_key("new-key") is not None


def test_failed_discovery_falls_back_to_the_known_jwks_location(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("old-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("old-key")

    del provider.documents[DISCOVERY]
    provider.publish("new-key")
    clock.advance(3600)

    assert cache.signing_key("new-key") is not None


def test_an_unknown_kid_refetches_the_keys(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("old-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("old-key")
    clock.advance(60)

    provider.publish("old-key", "rotated-key")

    assert cache.signing_key("rotated-key") is not None
    assert events.names() == ["jwks_fetched", "jwks_refetch"]
    assert events.events[-1][1]["kid"] == "rotated-key"


def test_unknown_kids_refetch_at_most_once_per_minute(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("pool-key")
    clock.advance(60)

    for attempt in range(50):
        assert cache.signing_key(f"random-{attempt}") is None
        clock.advance(1)

    # The first unknown kid refetched; the next 49 fell within the following 60 seconds.
    assert provider.jwks_fetches() == 1 + 1
    clock.advance(10)
    cache.signing_key("yet-another")
    assert provider.jwks_fetches() == 3


def test_a_refetch_right_after_the_first_load_is_suppressed(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    cache = cache_for(provider, clock, events)

    assert cache.signing_key("unknown") is None
    clock.advance(59)
    assert cache.signing_key("unknown") is None

    assert provider.jwks_fetches() == 1


def test_stale_keys_are_used_when_the_provider_is_unreachable(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("pool-key")

    provider.offline = True
    clock.advance(7200)

    assert cache.signing_key("pool-key") is not None
    name, fields = events.events[-1]
    assert name == "jwks_fetch_failed"
    assert fields["staleKeyCount"] == 1


def test_a_failed_refresh_is_retried_at_most_once_per_minute(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    cache = cache_for(provider, clock, events)
    cache.signing_key("pool-key")
    provider.offline = True
    clock.advance(3600)

    for _ in range(30):
        cache.signing_key("pool-key")
        clock.advance(1)
    assert provider.jwks_fetches() == 2

    provider.offline = False
    provider.publish("pool-key", "new-key")
    clock.advance(30)
    assert cache.signing_key("new-key") is not None


def test_no_keys_at_all_fails_closed(clock: FakeClock, events: RecordedEvents) -> None:
    provider = FakeProvider("pool-key")
    provider.offline = True
    cache = cache_for(provider, clock, events)

    with pytest.raises(SigningKeysUnavailable):
        cache.signing_key("pool-key")
    assert events.names() == ["jwks_fetch_failed"]


def test_without_keys_retries_are_spaced_and_still_recover_quickly(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    provider.offline = True
    cache = cache_for(provider, clock, events)
    with pytest.raises(SigningKeysUnavailable):
        cache.signing_key("pool-key")
    provider.offline = False

    # Within the backoff, requests (e.g. random kids) don't reach the provider.
    clock.advance(1)
    with pytest.raises(SigningKeysUnavailable):
        cache.signing_key("random-kid")
    assert provider.jwks_fetches() == 0

    clock.advance(4)
    assert cache.signing_key("pool-key") is not None


def test_an_empty_key_set_counts_as_a_failed_fetch(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider()

    cache = cache_for(provider, clock, events)

    with pytest.raises(SigningKeysUnavailable):
        cache.signing_key("pool-key")


def test_a_discovery_document_without_jwks_uri_counts_as_a_failed_fetch(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider("pool-key")
    provider.documents[DISCOVERY] = {"issuer": ISSUER}

    cache = cache_for(provider, clock, events)

    with pytest.raises(SigningKeysUnavailable):
        cache.signing_key("pool-key")


def test_keys_that_cannot_verify_rs256_are_ignored(
    clock: FakeClock, events: RecordedEvents
) -> None:
    provider = FakeProvider()
    provider.documents[JWKS_URI] = {
        "keys": [
            jwk("rsa-sig"),
            jwk("rsa-enc", use="enc"),
            {"kty": "oct", "kid": "hmac", "k": "c2VjcmV0"},
            {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
            {"kty": "RSA", "n": "AQAB", "e": "AQAB"},
        ]
    }
    cache = cache_for(provider, clock, events)

    assert cache.signing_key("rsa-sig") is not None
    for kid in ("rsa-enc", "hmac", "broken"):
        assert cache.signing_key(kid) is None


@pytest.fixture
def silent_server() -> Iterator[str]:
    """Accepts connections and never answers, like a provider that hangs."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    accepted: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted.append(server.accept()[0])
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/.well-known/jwks.json"
    stop.set()
    thread.join()
    for connection in accepted:
        connection.close()
    server.close()


def test_a_hanging_provider_times_out_after_two_seconds(silent_server: str) -> None:
    assert FETCH_TIMEOUT_SECONDS == 2

    started = time.monotonic()
    with pytest.raises(KeyFetchError):
        fetch_json(silent_server)

    assert 1.9 <= time.monotonic() - started < 3


def test_an_unreachable_provider_is_a_fetch_error() -> None:
    with pytest.raises(KeyFetchError):
        fetch_json("http://127.0.0.1:9/.well-known/openid-configuration")
