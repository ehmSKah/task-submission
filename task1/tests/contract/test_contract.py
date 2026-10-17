"""Generated requests against the deployed stack keep the OpenAPI contract (ADRs 0008 and 0011).

Each response is checked by Schemathesis: no `5xx`, only documented status codes, a documented
content type, headers and body matching their schemas, invalid requests rejected, and a token
required where the contract says so.
"""

import pytest
import schemathesis
from schemathesis import Case

from orders_api_infra.openapi_extensions import load_spec
from tests.contract.harness import StatusesSeen, success_statuses

schema = schemathesis.pytest.from_fixture("api_schema")


@schema.parametrize()
def test_every_response_keeps_the_contract(
    case: Case, caller: str, statuses_seen: StatusesSeen
) -> None:
    response = case.call()
    statuses_seen.record(caller, case.operation.label, response.status_code)
    case.validate_response(response)


def test_the_run_reached_every_operations_success(statuses_seen: StatusesSeen) -> None:
    """Otherwise the checks above only ever saw errors for some operation."""
    if not statuses_seen.lines():
        pytest.skip("runs after the generated requests, in the same session")
    assert statuses_seen.never_succeeded(success_statuses(load_spec())) == {}
