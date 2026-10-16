import httpx

from tests.integration.assertions import assert_contract_headers


def test_health_needs_no_token_and_reports_ok(api: httpx.Client) -> None:
    response = api.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json() == {"status": "ok"}
    assert_contract_headers(response)
