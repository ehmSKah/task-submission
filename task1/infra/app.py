"""CDK app entry point: `cdk deploy -c env=<env>` (ADR 0010)."""

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagPackSuppression, NagSuppressions

from orders_api_infra.config import load_config
from orders_api_infra.stack import OrdersApiStack

app = cdk.App()
config = load_config(app.node.try_get_context("env") or "local")
stack = OrdersApiStack(app, config.stack_name, config=config)

cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

# Every suppression names the resource it applies to and why (ADR 0012).
NagSuppressions.add_resource_suppressions(
    stack.api.function,
    [
        NagPackSuppression(
            id="AwsSolutions-L1",
            reason="Python 3.13 is pinned deliberately and verified on Floci (ADR 0009); "
            "a runtime upgrade is a conscious change.",
        )
    ],
)
api_suppressions = [
    NagPackSuppression(
        id="AwsSolutions-APIG2",
        reason="Request validators are declared inside the OpenAPI body "
        "(x-amazon-apigateway-request-validators), which cdk-nag cannot inspect (ADR 0007).",
    ),
    NagPackSuppression(
        id="AwsSolutions-APIG6",
        reason="Stage execution logging is off by design; the functions write structured "
        "logs instead (ADR 0012).",
    ),
]
if config.target == "floci":
    api_suppressions += [
        NagPackSuppression(
            id="AwsSolutions-APIG1",
            reason="Local Floci environment: API Gateway access logs are an AWS-environment "
            "setting (ADR 0012).",
        ),
        NagPackSuppression(
            id="AwsSolutions-APIG3",
            reason="Local Floci environment: no web ACL is associated yet; ADR 0006 defines "
            "the WAF edge layer.",
        ),
    ]
NagSuppressions.add_resource_suppressions(
    stack.api.rest_api, api_suppressions, apply_to_children=True
)

app.synth()
