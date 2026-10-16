import json

from orders_api.adapters.inbound.problems import ProblemType, problem_body


def test_problem_body_follows_rfc_9457_with_request_id() -> None:
    body = problem_body(
        ProblemType.NOT_FOUND,
        detail="Order not found",
        instance="/api/v1/orders/abc",
        request_id="req-1",
    )

    assert body == {
        "type": "urn:problem:orders-api:not-found",
        "title": "Resource not found",
        "status": 404,
        "detail": "Order not found",
        "instance": "/api/v1/orders/abc",
        "requestId": "req-1",
    }


def test_problem_body_omits_detail_when_not_given() -> None:
    body = problem_body(ProblemType.INTERNAL_ERROR, instance="/x", request_id="r")

    assert "detail" not in body
    assert body["status"] == 500


def test_every_problem_type_has_a_urn_title_and_status() -> None:
    for problem in ProblemType:
        assert problem.urn.startswith("urn:problem:orders-api:")
        assert problem.title
        assert 400 <= problem.status <= 599
        json.dumps(problem_body(problem, instance="/", request_id="r"))
