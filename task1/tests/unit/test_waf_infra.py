"""The WAF edge layer: a per-IP rate-based rule on the API stage, answering a custom 429
(ADRs 0006 and 0012)."""

import json
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Template

from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api.contract import load_contract
from orders_api_infra.waf import TEST_HEADER, TEST_HEADER_VALUE
from tests.unit.data_stack import config, synth


def web_acl(template: Template) -> dict[str, Any]:
    [acl] = template.find_resources("AWS::WAFv2::WebACL").values()
    properties: dict[str, Any] = acl["Properties"]
    return properties


def rules_by_name(template: Template) -> dict[str, dict[str, Any]]:
    return {rule["Name"]: rule for rule in web_acl(template)["Rules"]}


@pytest.fixture
def template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Template:
    return synth(config(wafRateLimit=1234, wafTestRuleLimit=None), tmp_path, monkeypatch)[2]


def test_a_regional_web_acl_lets_everything_else_through(template: Template) -> None:
    acl = web_acl(template)

    assert acl["Scope"] == "REGIONAL"
    assert acl["DefaultAction"] == {"Allow": {}}
    assert acl["Name"] == "orders-api-local"


def test_the_flood_rule_counts_requests_per_ip_over_five_minutes_at_the_configured_limit(
    template: Template,
) -> None:
    [rule] = rules_by_name(template).values()

    assert rule["Statement"] == {
        "RateBasedStatement": {"Limit": 1234, "AggregateKeyType": "IP", "EvaluationWindowSec": 300}
    }


def test_a_blocked_request_gets_a_429_with_a_static_retry_after_and_the_security_headers(
    template: Template,
) -> None:
    [rule] = rules_by_name(template).values()

    response = rule["Action"]["Block"]["CustomResponse"]
    assert response["ResponseCode"] == 429
    headers = {header["Name"]: header["Value"] for header in response["ResponseHeaders"]}
    assert headers == {"Retry-After": "300", **SECURITY_HEADERS}
    assert response["CustomResponseBodyKey"] in web_acl(template)["CustomResponseBodies"]


def test_the_429_body_is_the_rate_limited_problem_without_a_request_id(
    template: Template,
) -> None:
    [body] = web_acl(template)["CustomResponseBodies"].values()

    # WAF can only send text/plain, text/html or application/json (ADR 0006).
    assert body["ContentType"] == "APPLICATION_JSON"
    problem = json.loads(body["Content"])
    assert problem == {
        "type": "urn:problem:orders-api:rate-limited",
        "title": "Too many requests",
        "status": 429,
        "detail": problem["detail"],
    }
    schema = load_contract()["components"]["schemas"]["Problem"]
    assert set(problem) <= set(schema["properties"])
    assert set(schema["required"]) <= set(problem)


def test_waf_reports_its_own_metrics_but_keeps_no_sampled_requests(template: Template) -> None:
    """A sampled request holds the full client address and headers, the token included."""
    acl = web_acl(template)
    [rule] = acl["Rules"]

    for visibility in (acl["VisibilityConfig"], rule["VisibilityConfig"]):
        assert visibility["CloudWatchMetricsEnabled"] is True
        assert visibility["SampledRequestsEnabled"] is False


def test_the_web_acl_is_associated_with_the_api_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = synth(config(), tmp_path, monkeypatch)[2]
    [acl_id] = template.find_resources("AWS::WAFv2::WebACL")
    [stage_id] = template.find_resources("AWS::ApiGateway::Stage")
    [api_id] = template.find_resources("AWS::ApiGateway::RestApi")

    [association] = template.find_resources("AWS::WAFv2::WebACLAssociation").values()

    assert association["Properties"]["WebACLArn"] == {"Fn::GetAtt": [acl_id, "Arn"]}
    resource_arn = json.dumps(association["Properties"]["ResourceArn"])
    assert f'"Ref": "{api_id}"' in resource_arn
    assert f'"Ref": "{stage_id}"' in resource_arn
    assert ":apigateway:" in resource_arn and "/restapis/" in resource_arn
    template.has_output("WebAclArn", {})
    template.has_output("ApiStageArn", {})


def test_the_test_rule_counts_only_requests_carrying_the_test_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = synth(config(wafTestRuleLimit=10), tmp_path, monkeypatch)[2]
    rules = rules_by_name(template)

    statement = rules["per-ip-test"]["Statement"]["RateBasedStatement"]
    assert statement["Limit"] == 10
    assert statement["AggregateKeyType"] == "IP"
    assert statement["ScopeDownStatement"] == {
        "ByteMatchStatement": {
            "FieldToMatch": {"SingleHeader": {"Name": TEST_HEADER}},
            "PositionalConstraint": "EXACTLY",
            "SearchString": TEST_HEADER_VALUE,
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
        }
    }
    # The same answer as the flood rule's, so the test sees what a flood would.
    assert rules["per-ip-test"]["Action"] == rules["per-ip-flood"]["Action"]
    assert "ScopeDownStatement" not in rules["per-ip-flood"]["Statement"]["RateBasedStatement"]


def test_without_the_test_rule_only_the_flood_rule_exists(template: Template) -> None:
    assert list(rules_by_name(template)) == ["per-ip-flood"]
