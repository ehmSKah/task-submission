"""The test client's public IPv4 address, as the internet sees it (ADR 0011).

`aws_only` tests compare it with what API Gateway and WAF see behind the edge-optimized endpoint.
Two well-known echo services are asked in turn, AWS's own first; this is the suite's one
dependency outside the deployment. Every request in those tests goes over IPv4, so the address
asked for and the address that reaches the API are of the same family; how IPv6 clients are seen
is still to be verified (docs/verification.md).
"""

import ipaddress

import httpx
import pytest

ECHO_SERVICES = ("https://checkip.amazonaws.com", "https://api.ipify.org")
IPV4_ONLY = "0.0.0.0"  # binding to it makes every connection IPv4


def ipv4_transport() -> httpx.HTTPTransport:
    return httpx.HTTPTransport(local_address=IPV4_ONLY, retries=1)


def public_ipv4() -> str:
    failures: list[str] = []
    with httpx.Client(transport=ipv4_transport(), timeout=5) as client:
        for url in ECHO_SERVICES:
            try:
                response = client.get(url)
                response.raise_for_status()
                address = ipaddress.IPv4Address(response.text.strip())
            except (httpx.HTTPError, ValueError) as error:
                failures.append(f"{url}: {type(error).__name__}")
                continue
            if address.is_global:
                return str(address)
            failures.append(f"{url}: {address} is not a public address")
    pytest.fail(f"No IP echo service told this client's public IPv4 address: {failures}")
