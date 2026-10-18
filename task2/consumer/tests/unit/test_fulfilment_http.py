"""The fulfilment service over HTTP (ADR 0003): one attempt, its answer sorted into a response
class, within fixed timeouts and a deadline for the whole attempt."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from order_consumer.adapters.outbound.fulfilment_http import HttpFulfilmentClient
from order_consumer.application.ports import HandoverFailed, HandoverResult, ResponseClass
from order_consumer.config import FulfilmentTimeouts
from order_consumer.domain.downstream import DownstreamRequest

BASE_URL = "http://fulfilment.test"
ORDER_ID = "3f1c2a9e-8b4d-4c1e-9f2a-6d7e8f9a0b1c"
REQUEST = DownstreamRequest(idempotency_key=ORDER_ID, payload={"externalReference": ORDER_ID})


@pytest.fixture
def stub() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
def client() -> Iterator[HttpFulfilmentClient]:
    client = HttpFulfilmentClient(BASE_URL, "secret-token", FulfilmentTimeouts())
    yield client
    client.close()


def failure_of(client: HttpFulfilmentClient) -> HandoverFailed:
    with pytest.raises(HandoverFailed) as failed:
        client.hand_over(REQUEST)
    return failed.value


def test_a_201_completes_the_handover(stub: respx.MockRouter, client: HttpFulfilmentClient) -> None:
    route = stub.post("/fulfilment-orders").respond(
        201, json={"id": "ful-8812", "externalReference": ORDER_ID}
    )

    result = client.hand_over(REQUEST)

    assert result == HandoverResult(fulfilment_order_id="ful-8812")
    sent = route.calls.last.request
    assert sent.headers["Idempotency-Key"] == ORDER_ID
    assert sent.headers["Authorization"] == "Bearer secret-token"
    assert json.loads(sent.content) == REQUEST.payload


@pytest.mark.parametrize(
    ("status", "response_class"),
    [
        (408, ResponseClass.TRANSIENT),
        (409, ResponseClass.TRANSIENT),  # the first request is still in progress
        (429, ResponseClass.TRANSIENT),
        (500, ResponseClass.TRANSIENT),
        (502, ResponseClass.TRANSIENT),
        (503, ResponseClass.TRANSIENT),
        (504, ResponseClass.TRANSIENT),
        (507, ResponseClass.TRANSIENT),
        (400, ResponseClass.REJECTED),
        (410, ResponseClass.REJECTED),
        (413, ResponseClass.REJECTED),
        (422, ResponseClass.REJECTED),
        (401, ResponseClass.SYSTEMIC),
        (403, ResponseClass.SYSTEMIC),
        (404, ResponseClass.SYSTEMIC),
        (405, ResponseClass.SYSTEMIC),
        (415, ResponseClass.SYSTEMIC),
        (200, ResponseClass.SYSTEMIC),
        (202, ResponseClass.SYSTEMIC),
        (204, ResponseClass.SYSTEMIC),
        (301, ResponseClass.SYSTEMIC),
        (304, ResponseClass.SYSTEMIC),
    ],
)
def test_every_other_answer_fails_the_attempt_with_its_response_class(
    stub: respx.MockRouter,
    client: HttpFulfilmentClient,
    status: int,
    response_class: ResponseClass,
) -> None:
    stub.post("/fulfilment-orders").respond(status)

    failure = failure_of(client)

    assert failure.response_class is response_class
    assert failure.status == status
    assert failure.error == f"{status} {httpx.codes.get_reason_phrase(status)}"


def test_a_redirect_is_not_followed(stub: respx.MockRouter, client: HttpFulfilmentClient) -> None:
    route = stub.post("/fulfilment-orders").respond(307, headers={"Location": "/elsewhere"})
    elsewhere = stub.post("/elsewhere").respond(201, json={"id": "ful-1"})

    failure = failure_of(client)

    assert failure.response_class is ResponseClass.SYSTEMIC
    assert (route.call_count, elsewhere.call_count) == (1, 0)


def test_a_rejection_carries_the_start_of_its_problem_body(
    stub: respx.MockRouter, client: HttpFulfilmentClient
) -> None:
    body = json.dumps({"title": "Bad Request", "detail": "x" * 5000})
    stub.post("/fulfilment-orders").respond(
        400, content=body, headers={"Content-Type": "application/problem+json"}
    )

    failure = failure_of(client)

    assert failure.problem is not None
    assert 0 < len(failure.problem) <= 1024
    assert body.startswith(failure.problem)


def test_a_transient_answer_carries_no_body(
    stub: respx.MockRouter, client: HttpFulfilmentClient
) -> None:
    stub.post("/fulfilment-orders").respond(503, json={"title": "Service Unavailable"})

    assert failure_of(client).problem is None


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        pytest.param("7", 7.0, id="delay in seconds"),
        pytest.param("120", 120.0, id="a long delay"),
        pytest.param("soon", None, id="unreadable"),
        pytest.param("-3", None, id="negative"),
        pytest.param(
            format_datetime(datetime(2001, 1, 1, tzinfo=UTC), usegmt=True),
            0.0,
            id="a date in the past",
        ),
    ],
)
def test_retry_after_is_read_from_a_transient_answer(
    stub: respx.MockRouter, client: HttpFulfilmentClient, header: str, seconds: float | None
) -> None:
    stub.post("/fulfilment-orders").respond(503, headers={"Retry-After": header})

    assert failure_of(client).retry_after == seconds


def test_retry_after_can_be_an_http_date(
    stub: respx.MockRouter, client: HttpFulfilmentClient
) -> None:
    later = datetime.now(UTC) + timedelta(seconds=30)
    stub.post("/fulfilment-orders").respond(
        429, headers={"Retry-After": format_datetime(later, usegmt=True)}
    )

    retry_after = failure_of(client).retry_after

    assert retry_after is not None and 25.0 <= retry_after <= 30.0


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (httpx.ConnectError("connection refused"), "ConnectError"),
        (httpx.ConnectTimeout("connect timed out"), "ConnectTimeout"),
        (httpx.ReadTimeout("read timed out"), "ReadTimeout"),
        (httpx.ReadError("connection reset by peer"), "ReadError"),
        (httpx.RemoteProtocolError("server disconnected"), "RemoteProtocolError"),
    ],
)
def test_no_answer_is_transient(
    stub: respx.MockRouter, client: HttpFulfilmentClient, error: Exception, name: str
) -> None:
    stub.post("/fulfilment-orders").mock(side_effect=error)

    failure = failure_of(client)

    assert failure.response_class is ResponseClass.TRANSIENT
    assert failure.status is None
    assert failure.error.startswith(name)


def test_an_attempt_that_outlasts_its_deadline_is_transient(stub: respx.MockRouter) -> None:
    """However the time is spent, one attempt ends at its deadline (ADR 0003)."""
    answer = threading.Event()

    def slow(_request: httpx.Request) -> httpx.Response:
        answer.wait(timeout=10)
        return httpx.Response(201, json={"id": "ful-1"})

    stub.post("/fulfilment-orders").mock(side_effect=slow)
    client = HttpFulfilmentClient(
        BASE_URL, "secret-token", FulfilmentTimeouts(attempt_deadline=0.2)
    )
    try:
        failure = failure_of(client)
    finally:
        answer.set()
        client.close()

    assert failure.response_class is ResponseClass.TRANSIENT
    assert failure.status is None
    assert failure.error == "no answer within the attempt deadline of 0.2 s"
