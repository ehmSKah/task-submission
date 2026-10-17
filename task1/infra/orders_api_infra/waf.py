"""The edge layer: a regional WAF web ACL on the API stage with a per-IP rate-based rule (ADR 0006).

WAF runs before the authorizer, so it also stops floods of unauthenticated and invalid-token
requests, which never reach the API function's own limits. On Floci the web ACL is deployed and
can be read back through the WAF API, but nothing is enforced.
"""

import json
from collections.abc import Sequence

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from orders_api.adapters.inbound.cors import static_cors_headers
from orders_api.adapters.inbound.http_headers import SECURITY_HEADERS
from orders_api.adapters.inbound.problems import ProblemType, problem_body
from orders_api_infra.config import EnvConfig

EVALUATION_WINDOW_SECONDS = 300
# An address that pauses for a whole window has nothing left in it, whatever the limit, so the
# static `Retry-After` is the window itself.
RETRY_AFTER_SECONDS = EVALUATION_WINDOW_SECONDS

FLOOD_RULE = "per-ip-flood"
# Test-only (testFixtures): counts only requests carrying the test header, so the `aws_only` WAF
# test can trip a low limit while the other suites' requests from the same address never count.
TEST_RULE = "per-ip-test"
TEST_HEADER = "x-orders-api-waf-test"
TEST_HEADER_VALUE = "count-me"

RATE_LIMITED_BODY = "rate-limited"
RATE_LIMITED_DETAIL = (
    "Too many requests from this address. Retry after the number of seconds in Retry-After."
)


def rate_limited_problem() -> dict[str, object]:
    """The Problem Details WAF answers a blocked request with. WAF can't insert API Gateway's
    request id, so unlike every other error body it carries none."""
    return problem_body(ProblemType.RATE_LIMITED, request_id=None, detail=RATE_LIMITED_DETAIL)


def rate_limited_headers(cors_allowed_origins: Sequence[str]) -> dict[str, str]:
    """The custom response's headers. WAF sets `Content-Type` itself from the body's content type
    and refuses it as a custom header, so it is `application/json` here (ADR 0006). They are
    static, so they name an allowed browser origin only where there is exactly one (ADR 0014)."""
    return {
        "Retry-After": str(RETRY_AFTER_SECONDS),
        **SECURITY_HEADERS,
        **static_cors_headers(cors_allowed_origins),
    }


def _rule(
    name: str,
    priority: int,
    limit: int,
    scope_down: wafv2.CfnWebACL.StatementProperty | None,
    headers: dict[str, str],
) -> wafv2.CfnWebACL.RuleProperty:
    # Aggregated by the address WAF sees, never by a forwarded-IP header (ADR 0006).
    return wafv2.CfnWebACL.RuleProperty(
        name=name,
        priority=priority,
        statement=wafv2.CfnWebACL.StatementProperty(
            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                limit=limit,
                aggregate_key_type="IP",
                evaluation_window_sec=EVALUATION_WINDOW_SECONDS,
                scope_down_statement=scope_down,
            )
        ),
        action=wafv2.CfnWebACL.RuleActionProperty(
            block=wafv2.CfnWebACL.BlockActionProperty(
                custom_response=wafv2.CfnWebACL.CustomResponseProperty(
                    response_code=ProblemType.RATE_LIMITED.status,
                    custom_response_body_key=RATE_LIMITED_BODY,
                    response_headers=[
                        wafv2.CfnWebACL.CustomHTTPHeaderProperty(name=header, value=value)
                        for header, value in headers.items()
                    ],
                )
            )
        ),
        visibility_config=_visibility(name),
    )


def _test_header_matches() -> wafv2.CfnWebACL.StatementProperty:
    return wafv2.CfnWebACL.StatementProperty(
        byte_match_statement=wafv2.CfnWebACL.ByteMatchStatementProperty(
            field_to_match=wafv2.CfnWebACL.FieldToMatchProperty(
                single_header={"Name": TEST_HEADER}
            ),
            positional_constraint="EXACTLY",
            search_string=TEST_HEADER_VALUE,
            text_transformations=[
                wafv2.CfnWebACL.TextTransformationProperty(priority=0, type="NONE")
            ],
        )
    )


def _visibility(metric_name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    # WAF's own metrics, no custom ones (ADR 0012). No sampled requests: WAF keeps a sample's
    # full client address and headers, `Authorization` included, which ADR 0012 never stores.
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        sampled_requests_enabled=False,
        metric_name=metric_name,
    )


class WebAcl(Construct):
    def __init__(
        self, scope: Construct, construct_id: str, *, config: EnvConfig, stage: apigateway.Stage
    ) -> None:
        super().__init__(scope, construct_id)
        headers = rate_limited_headers(config.cors_allowed_origins)
        rules = [_rule(FLOOD_RULE, 0, config.waf_rate_limit, None, headers)]
        if config.waf_test_rule_limit is not None:
            rules.append(
                _rule(TEST_RULE, 1, config.waf_test_rule_limit, _test_header_matches(), headers)
            )
        name = f"orders-api-{config.name}"
        self.web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            name=name,
            description="Per-IP flood protection in front of the Orders API, ADR 0006",
            scope="REGIONAL",  # also for the edge-optimized endpoint
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            custom_response_bodies={
                RATE_LIMITED_BODY: wafv2.CfnWebACL.CustomResponseBodyProperty(
                    content_type="APPLICATION_JSON",
                    content=json.dumps(rate_limited_problem(), separators=(",", ":")),
                )
            },
            rules=rules,
            visibility_config=_visibility(name),
        )
        self.association = wafv2.CfnWebACLAssociation(
            self,
            "Association",
            web_acl_arn=self.web_acl.attr_arn,
            resource_arn=stage.stage_arn,
        )

        cdk.CfnOutput(self, "WebAclArn", value=self.web_acl.attr_arn, key="WebAclArn")
        cdk.CfnOutput(self, "ApiStageArn", value=stage.stage_arn, key="ApiStageArn")
