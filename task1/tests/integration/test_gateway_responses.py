"""Errors produced by API Gateway itself use the same Problem Details contract (ADR 0007)."""

import httpx

from tests.integration.assertions import assert_problem


def test_unknown_path_is_a_problem_404_not_the_gateway_403(api: httpx.Client) -> None:
    response = api.get("/api/v1/does-not-exist")

    assert_problem(response, 404, "not-found")


def test_unknown_method_on_a_known_path_is_a_problem_404(api: httpx.Client) -> None:
    response = api.post("/api/v1/health")

    assert_problem(response, 404, "not-found")
