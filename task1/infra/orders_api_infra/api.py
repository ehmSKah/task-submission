"""The Api construct: the authorizer, the API function, the REST API built from OpenAPI and the
WAF web ACL in front of it."""

import json

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from orders_api.adapters.outbound.dynamodb_orders import BY_CREATED_INDEX, BY_OWNER_INDEX
from orders_api_infra.config import Architecture, EnvConfig
from orders_api_infra.openapi_extensions import load_spec, with_aws_extensions
from orders_api_infra.paths import BUNDLE_DIR
from orders_api_infra.waf import WebAcl

ARCHITECTURES = {
    Architecture.ARM_64: lambda_.Architecture.ARM_64,
    Architecture.X86_64: lambda_.Architecture.X86_64,
}
HANDLERS = "orders_api.adapters.inbound"

RETENTION = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
}

# API Gateway's access log line: one JSON object per request, every value quoted so that a missing
# one (`-`) still parses. It names no header, so neither the `Authorization` header nor a token can
# end up in it; the full client address is kept here only, under the log retention (ADR 0012).
ACCESS_LOG_FIELDS = {
    "requestId": "$context.requestId",
    "requestTime": "$context.requestTimeEpoch",
    "httpMethod": "$context.httpMethod",
    "resourcePath": "$context.resourcePath",
    "protocol": "$context.protocol",
    "status": "$context.status",
    "responseLatency": "$context.responseLatency",
    "integrationLatency": "$context.integrationLatency",
    "sourceIp": "$context.identity.sourceIp",
    "errorType": "$context.error.responseType",
    "authorizerError": "$context.authorizer.error",
    "wafResponseCode": "$context.wafResponseCode",
}


class Api(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        authorizer_environment: dict[str, str],
        orders_table: dynamodb.ITable,
        idempotency_table: dynamodb.ITable,
        rate_limit_table: dynamodb.ITable,
    ) -> None:
        super().__init__(scope, construct_id)
        self._config = config
        self._code = lambda_.Code.from_asset(str(BUNDLE_DIR))  # one bundle, two functions

        # Answers "who is this?"; needs no AWS permissions beyond its logs (ADR 0012).
        self.authorizer_function = self._function(
            "AuthorizerFunction",
            handler=f"{HANDLERS}.authorizer_handler.handler",
            timeout_seconds=5,
            environment=authorizer_environment,
        )
        self.api_function = self._function(
            "ApiFunction",
            handler=f"{HANDLERS}.api_handler.handler",
            timeout_seconds=10,
            environment={
                "ORDERS_TABLE": orders_table.table_name,
                "IDEMPOTENCY_TABLE": idempotency_table.table_name,
                "RATE_LIMIT_TABLE": rate_limit_table.table_name,
                "RATE_LIMITS_PARAMETER": config.rate_limits_parameter,
                "RATE_LIMITS_MAX_AGE_SECONDS": str(config.rate_limit_config_cache_seconds),
            },
        )
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[orders_table.table_arn],
            )
        )
        # Lists read the two indexes only, never the table itself (ADR 0005).
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[
                    f"{orders_table.table_arn}/index/{index}"
                    for index in (BY_OWNER_INDEX, BY_CREATED_INDEX)
                ],
            )
        )
        # Exactly the calls Powertools Idempotency makes, on this one table (ADR 0012).
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[idempotency_table.table_arn],
            )
        )

        # Each rate-limit check is one conditional UpdateItem; nothing else touches counters.
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:UpdateItem"], resources=[rate_limit_table.table_arn]
            )
        )
        # Powertools Parameters reads the one parameter with the limits (ADR 0006).
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    cdk.Stack.of(self).format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=config.rate_limits_parameter.removeprefix("/"),
                    )
                ],
            )
        )

        self.rest_api = apigateway.SpecRestApi(
            self,
            "RestApi",
            rest_api_name=f"orders-api-{config.name}",
            api_definition=apigateway.ApiDefinition.from_inline(
                with_aws_extensions(
                    load_spec(),
                    api_integration_uri=self._invocation_uri(self.api_function),
                    authorizer_uri=self._invocation_uri(self.authorizer_function),
                    target=config.target,
                )
            ),
            endpoint_types=[apigateway.EndpointType.EDGE],
            deploy_options=self._stage_options(),
            # The account's API Gateway logging role is a per-account, per-region setting made
            # once outside this stack, like CDK's bootstrap (ADR 0010).
            cloud_watch_role=False,
        )
        self.web_acl = WebAcl(self, "WebAcl", config=config, stage=self.rest_api.deployment_stage)

        # Only this API may invoke either function (ADRs 0003 and 0012).
        stack = cdk.Stack.of(self)
        self.authorizer_function.add_permission(
            "InvokeFromRestApiAuthorizer",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=(
                f"arn:{stack.partition}:execute-api:{stack.region}:{stack.account}:"
                f"{self.rest_api.rest_api_id}/authorizers/*"
            ),
        )
        self.api_function.add_permission(
            "InvokeFromRestApi",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=self.rest_api.arn_for_execute_api(stage=config.stage_name),
        )

        cdk.CfnOutput(self, "ApiId", value=self.rest_api.rest_api_id, key="ApiId")
        cdk.CfnOutput(self, "StageName", value=config.stage_name, key="StageName")

    def _stage_options(self) -> apigateway.StageOptions:
        config = self._config
        if not config.access_logs:
            return apigateway.StageOptions(stage_name=config.stage_name)
        self.access_log_group = logs.LogGroup(
            self,
            "AccessLogs",
            retention=RETENTION[config.log_retention_days],
            removal_policy=config.data_removal_policy,
        )
        return apigateway.StageOptions(
            stage_name=config.stage_name,
            access_log_destination=apigateway.LogGroupLogDestination(self.access_log_group),
            access_log_format=apigateway.AccessLogFormat.custom(
                json.dumps(ACCESS_LOG_FIELDS, separators=(",", ":"))
            ),
        )

    def _function(
        self, construct_id: str, *, handler: str, timeout_seconds: int, environment: dict[str, str]
    ) -> lambda_.Function:
        config = self._config
        log_group = logs.LogGroup(
            self,
            f"{construct_id}Logs",
            retention=RETENTION[config.log_retention_days],
            removal_policy=config.data_removal_policy,
        )
        role = iam.Role(
            self,
            f"{construct_id}Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"Orders API {construct_id}: only what it uses (ADR 0012)",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[log_group.log_group_arn],
            )
        )
        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=ARCHITECTURES[config.resolved_architecture()],
            handler=handler,
            code=self._code,
            memory_size=256,
            timeout=cdk.Duration.seconds(timeout_seconds),
            role=role,
            log_group=log_group,
            environment={
                "POWERTOOLS_LOG_LEVEL": config.log_level,
                "ORDERS_API_ENV": config.name,
                **environment,
            },
        )

    @staticmethod
    def _invocation_uri(function: lambda_.IFunction) -> str:
        stack = cdk.Stack.of(function)
        return (
            f"arn:{stack.partition}:apigateway:{stack.region}:lambda:path/2015-03-31/functions/"
            f"{function.function_arn}/invocations"
        )
