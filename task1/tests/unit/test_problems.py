import json

from orders_api.adapters.inbound.problems import (
    FieldError,
    ProblemType,
    json_pointer,
    problem_body,
)


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


def test_field_errors_point_into_the_body_or_name_a_parameter() -> None:
    body = problem_body(
        ProblemType.VALIDATION_ERROR,
        detail="Duplicate SKUs",
        request_id="r",
        errors=[
            FieldError(detail="duplicate SKU ABC-1", pointer="/items/1/sku"),
            FieldError(detail="must be a UUID v4", parameter="id"),
        ],
    )

    assert body["errors"] == [
        {"pointer": "/items/1/sku", "detail": "duplicate SKU ABC-1"},
        {"parameter": "id", "detail": "must be a UUID v4"},
    ]


def test_problem_body_omits_errors_when_there_are_none() -> None:
    assert "errors" not in problem_body(ProblemType.VALIDATION_ERROR, request_id="r", errors=[])


def test_json_pointer_escapes_tilde_and_slash() -> None:
    assert json_pointer(["items", 0, "sku"]) == "/items/0/sku"
    assert json_pointer(["a/b", "c~d"]) == "/a~1b/c~0d"
    assert json_pointer([]) == ""
