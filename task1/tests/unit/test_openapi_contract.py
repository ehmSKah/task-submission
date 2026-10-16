from orders_api.adapters.inbound.problems import ProblemType
from orders_api_infra.openapi_extensions import load_spec


def test_the_contract_documents_exactly_the_problem_type_catalogue() -> None:
    documented = load_spec()["components"]["schemas"]["Problem"]["properties"]["type"]["enum"]

    assert set(documented) == {problem.urn for problem in ProblemType} | {"about:blank"}
