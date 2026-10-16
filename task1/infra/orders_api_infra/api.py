"""The Api construct: the authorizer, the API function and the REST API built from OpenAPI."""

import aws_cdk as cdk
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_dynamodb as dynamodb
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
HANDLERS = "orders_api.adapters.inbound"

RETENTION = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    90: logs.RetentionDays.THREE_MONTHS,
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
            },
        )
        self.api_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[orders_table.table_arn],
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
            deploy_options=apigateway.StageOptions(stage_name=config.stage_name),
            cloud_watch_role=False,
        )

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
