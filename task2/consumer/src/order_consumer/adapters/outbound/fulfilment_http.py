"""The fulfilment service over HTTP (ADR 0003).

One call is one attempt: its answer is sorted into a response class here, so the core never sees a
status code, and replacing the contract touches only this adapter and mapping 2.
"""

import contextvars
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from order_consumer.application.ports import HandoverFailed, HandoverResult, ResponseClass
from order_consumer.config import FulfilmentTimeouts
from order_consumer.domain.downstream import DownstreamRequest

# Wrong for every order: our configuration or the service is broken (ADR 0003).
SYSTEMIC_STATUSES = frozenset({401, 403, 404, 405, 415})
# Worth trying again; 409 means the first request with this key is still in progress.
TRANSIENT_STATUSES = frozenset({408, 409, 429})
# How much of a rejection's body a dead letter keeps.
PROBLEM_CHARACTERS = 1024


def response_class(status: int) -> ResponseClass:
    if status == httpx.codes.CREATED:
        return ResponseClass.SUCCESS
    if status < 400 or status in SYSTEMIC_STATUSES:
        return ResponseClass.SYSTEMIC  # any other 2xx, any 3xx (redirects are not followed)
    if status in TRANSIENT_STATUSES or status >= 500:
        return ResponseClass.TRANSIENT
    return ResponseClass.REJECTED


def retry_after(response: httpx.Response) -> float | None:
    """Seconds to wait, from delay-seconds or an HTTP date; None if missing or unreadable."""
    value = response.headers.get("Retry-After", "").strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


class HttpFulfilmentClient:
    def __init__(self, base_url: str, token: str, timeouts: FulfilmentTimeouts) -> None:
        self._deadline = timeouts.attempt_deadline
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(
                connect=timeouts.connect,
                read=timeouts.read,
                write=timeouts.read,
                pool=timeouts.connect,
            ),
            # No redirects, and no retries inside the client: the consumer's retry policy is the
            # only layer that retries.
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        )

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        response = self._within_deadline(request)
        status = response.status_code
        match response_class(status):
            case ResponseClass.SUCCESS:
                return HandoverResult(fulfilment_order_id=_fulfilment_order_id(response))
            case ResponseClass.TRANSIENT as transient:
                raise HandoverFailed(
                    transient,
                    _status_line(response),
                    status=status,
                    retry_after=retry_after(response),
                )
            case ResponseClass.REJECTED as rejected:
                raise HandoverFailed(
                    rejected,
                    _status_line(response),
                    status=status,
                    problem=response.text[:PROBLEM_CHARACTERS],
                )
            case systemic:
                raise HandoverFailed(systemic, _status_line(response), status=status)

    def _within_deadline(self, request: DownstreamRequest) -> httpx.Response:
        """The per-phase timeouts bound each step, not the whole attempt, so the attempt runs in
        its own thread and is abandoned at its deadline. An abandoned request ends within its own
        timeouts; whatever it achieved, the idempotency key makes the next attempt safe."""
        answer: Future[httpx.Response] = Future()
        context = contextvars.copy_context()  # the message's log context

        def attempt() -> None:
            try:
                answer.set_result(context.run(self._post, request))
            except BaseException as error:
                answer.set_exception(error)

        threading.Thread(target=attempt, name="handover-attempt", daemon=True).start()
        try:
            return answer.result(timeout=self._deadline)
        except FutureTimeout:
            raise HandoverFailed(
                ResponseClass.TRANSIENT,
                f"no answer within the attempt deadline of {self._deadline:g} s",
            ) from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as error:
            # Connection refused or reset, a timeout, a connection dropped mid-answer.
            raise HandoverFailed(
                ResponseClass.TRANSIENT, f"{type(error).__name__}: {str(error)[:200]}"
            ) from error

    def _post(self, request: DownstreamRequest) -> httpx.Response:
        return self._client.post(
            "/fulfilment-orders",
            json=request.payload,
            headers={"Idempotency-Key": request.idempotency_key},
        )

    def close(self) -> None:
        self._client.close()


def _fulfilment_order_id(response: httpx.Response) -> str:
    """Without it there is no proof of the handover; a service answering like this is broken for
    every order, so the answer is systemic."""
    try:
        fulfilment_order_id = response.json()["id"]
    except (ValueError, TypeError, KeyError):
        fulfilment_order_id = None
    if not isinstance(fulfilment_order_id, str) or not fulfilment_order_id:
        raise HandoverFailed(
            ResponseClass.SYSTEMIC,
            f"{_status_line(response)} without a fulfilment order id",
            status=response.status_code,
        )
    return fulfilment_order_id


def _status_line(response: httpx.Response) -> str:
    return f"{response.status_code} {response.reason_phrase}"
