"""Integration tests run over HTTP against the deployed stack named in build/endpoints.json."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

ENDPOINTS = Path(__file__).parents[2] / "build" / "endpoints.json"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    if not ENDPOINTS.is_file():
        pytest.fail("build/endpoints.json is missing: run `make up` first")
    base_url = json.loads(ENDPOINTS.read_text())["apiUrl"]
    with httpx.Client(base_url=base_url, timeout=30) as client:
        yield client
