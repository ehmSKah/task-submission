"""Adapter tests call the real outbound adapters against the emulator (ADR 0011)."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.adapter)
