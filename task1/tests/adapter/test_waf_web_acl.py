"""The deployed web ACL, read back through the WAF API (ADR 0006).

Floci keeps WAF resources but filters no traffic, so this proves what CloudFormation created:
the rules, their limits and key, the custom response and the association with the stage. That
WAF really blocks is an `aws_only` integration test.
"""

import base64
import json
from typing import Any

import boto3
import pytest

from orders_api_infra.config import load_config
from orders_api_infra.waf import (
    EVALUATION_WINDOW_SECONDS,
    FLOOD_RULE,
    TEST_HEADER,
    TEST_HEADER_VALUE,
    TEST_RULE,
    rate_limited_headers,
    rate_limited_problem,
)


def _encode_search_strings(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "SearchString" and isinstance(value, str):
                node[key] = base64.b64encode(value.encode()).decode()
            else:
                _encode_search_strings(value)
    elif isinstance(node, list):
        for value in node:
            _encode_search_strings(value)


def _as_aws_sends_it(response_dict: dict[str, Any], **_: Any) -> None:
    """Floci 2.3.0 returns a byte match's `SearchString` as plain text, where AWS sends the
    blob base64-encoded and botocore decodes it; encode it before botocore parses the answer
    (docs/verification.md)."""
    body = json.loads(response_dict["body"])
    _encode_search_strings(body)
    response_dict["body"] = json.dumps(body).encode()


def wafv2_client(endpoints: dict[str, Any]) -> Any:
    waf = boto3.client("wafv2")
    if endpoints["target"] == "floci":
        for operation in ("GetWebACL", "GetWebACLForResource"):
            waf.meta.events.register(f"before-parse.wafv2.{operation}", _as_aws_sends_it)
    return waf


@pytest.fixture(scope="module")
def web_acl(endpoints: dict[str, Any]) -> dict[str, Any]:
    # arn:aws:wafv2:<region>:<account>:regional/webacl/<name>/<id>
    *_, name, acl_id = endpoints["webAclArn"].split("/")
    found = wafv2_client(endpoints).get_web_acl(Name=name, Scope="REGIONAL", Id=acl_id)
    acl: dict[str, Any] = found["WebACL"]
    return acl


def rules(web_acl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {rule["Name"]: rule for rule in web_acl["Rules"]}


def test_the_flood_rule_limits_each_ip_over_five_minutes(
    web_acl: dict[str, Any], endpoints: dict[str, Any]
) -> None:
    statement = rules(web_acl)[FLOOD_RULE]["Statement"]["RateBasedStatement"]

    assert statement["Limit"] == load_config(endpoints["env"]).waf_rate_limit
    assert statement["AggregateKeyType"] == "IP"
    assert statement["EvaluationWindowSec"] == EVALUATION_WINDOW_SECONDS
    assert "ForwardedIPConfig" not in statement
    assert web_acl["DefaultAction"] == {"Allow": {}}


def test_blocked_requests_get_the_rate_limited_problem_as_a_429(
    web_acl: dict[str, Any], endpoints: dict[str, Any]
) -> None:
    response = rules(web_acl)[FLOOD_RULE]["Action"]["Block"]["CustomResponse"]

    assert response["ResponseCode"] == 429
    headers = {header["Name"]: header["Value"] for header in response["ResponseHeaders"]}
    assert headers == rate_limited_headers(load_config(endpoints["env"]).cors_allowed_origins)
    body = web_acl["CustomResponseBodies"][response["CustomResponseBodyKey"]]
    assert body["ContentType"] == "APPLICATION_JSON"
    assert json.loads(body["Content"]) == rate_limited_problem()


def test_the_test_rule_counts_only_requests_with_the_test_header(
    web_acl: dict[str, Any], endpoints: dict[str, Any]
) -> None:
    config = load_config(endpoints["env"])
    if config.waf_test_rule_limit is None:
        pytest.skip(f"environment '{config.name}' has no WAF test rule")

    rule = rules(web_acl)[TEST_RULE]
    statement = rule["Statement"]["RateBasedStatement"]

    assert statement["Limit"] == config.waf_test_rule_limit
    assert statement["AggregateKeyType"] == "IP"
    match = statement["ScopeDownStatement"]["ByteMatchStatement"]
    assert match["FieldToMatch"] == {"SingleHeader": {"Name": TEST_HEADER}}
    assert match["PositionalConstraint"] == "EXACTLY"
    assert match["SearchString"] == TEST_HEADER_VALUE.encode()
    assert rule["Action"] == rules(web_acl)[FLOOD_RULE]["Action"]


def test_the_web_acl_is_associated_with_the_api_stage(
    web_acl: dict[str, Any], endpoints: dict[str, Any]
) -> None:
    waf = wafv2_client(endpoints)

    found = waf.get_web_acl_for_resource(ResourceArn=endpoints["apiStageArn"])

    assert found["WebACL"]["ARN"] == endpoints["webAclArn"] == web_acl["ARN"]
    associated = waf.list_resources_for_web_acl(
        WebACLArn=endpoints["webAclArn"], ResourceType="API_GATEWAY"
    )
    assert associated["ResourceArns"] == [endpoints["apiStageArn"]]
    *_, api_id, _, stage = endpoints["apiStageArn"].split("/")
    assert api_id in endpoints["apiUrl"]
    assert stage == load_config(endpoints["env"]).stage_name
