"""The Api construct: the API function and the REST API built from the OpenAPI document."""

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from orders_api_infra.config import Architecture, EnvConfig
from orders_api_infra.openapi_extensions import load_spec, with_aws_extensions
from orders_api_infra.paths import BUNDLE_DIR

ARCHITECTURES = {
    Architecture.ARM_64: lambda_.Architecture.ARM_64,
    Architecture.X86_64: lambda_.Architecture.X86_64,
}


class Api(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: EnvConfig) -> None:
        super().__init__(scope, construct_id)
        stack = cdk.Stack.of(self)
        removal = cdk.RemovalPolicy.RETAIN if config.retains_data else cdk.RemovalPolicy.DESTROY

        log_group = logs.LogGroup(
            self,
            "ApiFunctionLogs",
            retention=RETENTION[config.log_retention_days],
            removal_policy=removal,
        )
        role = iam.Role(
            self,
            "ApiFunctionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Orders API function: only what it uses (ADR 0012)",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        )

        self.function = lambda_.Function(
            self,
            "ApiFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=ARCHITECTURES[config.resolved_architecture()],
            handler="orders_api.adapters.inbound.api_handler.handler",
            code=lambda_.Code.from_asset(str(BUNDLE_DIR)),
            memory_size=256,
            timeout=cdk.Duration.seconds(10),
            role=role,
            log_group=log_group,
            environment={
                "POWERTOOLS_SERVICE_NAME": "orders-api",
                "POWERTOOLS_LOG_LEVEL": config.log_level,
                "ORDERS_API_ENV": config.name,
            },
        )

        integration_uri = (
            f"arn:{stack.partition}:apigateway:{stack.region}:lambda:path/2015-03-31/functions/"
            f"{self.function.function_arn}/invocations"
        )
        self.rest_api = apigateway.SpecRestApi(
            self,
            "RestApi",
            rest_api_name=f"orders-api-{config.name}",
            api_definition=apigateway.ApiDefinition.from_inline(
                with_aws_extensions(load_spec(), api_integration_uri=integration_uri)
            ),
            endpoint_types=[apigateway.EndpointType.EDGE],
            deploy_options=apigateway.StageOptions(stage_name=config.stage_name),
            cloud_watch_role=False,
        )

        # Only this API may invoke the function (ADRs 0003 and 0012).
        self.function.add_permission(
            "InvokeFromRestApi",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=self.rest_api.arn_for_execute_api(stage=config.stage_name),
        )

        cdk.CfnOutput(self, "ApiId", value=self.rest_api.rest_api_id, key="ApiId")
        cdk.CfnOutput(self, "StageName", value=config.stage_name, key="StageName")


RETENTION = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
}
