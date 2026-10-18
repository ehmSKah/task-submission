"""The fulfilment service over HTTP (ADR 0003)."""

import httpx

from order_consumer.application.ports import HandoverResult
from order_consumer.domain.downstream import DownstreamRequest

# One attempt (ADR 0003): connect 2 s, read 5 s. No redirects, and no retries inside the client:
# the consumer's retry policy is the only layer that retries.
TIMEOUT = httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)


class FulfilmentError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"fulfilment service answered {status}")
        self.status = status


class HttpFulfilmentClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=httpx.HTTPTransport(retries=0),
        )

    def hand_over(self, request: DownstreamRequest) -> HandoverResult:
        response = self._client.post(
            "/fulfilment-orders",
            json=request.payload,
            headers={"Idempotency-Key": request.idempotency_key},
        )
        if response.status_code != httpx.codes.CREATED:
            raise FulfilmentError(response.status_code)
        return HandoverResult(fulfilment_order_id=response.json()["id"])

    def close(self) -> None:
        self._client.close()
