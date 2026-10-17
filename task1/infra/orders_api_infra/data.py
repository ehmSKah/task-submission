"""The Data construct: one table per purpose (ADR 0005), and the runtime rate limits.

- orders, with its two list indexes
- idempotency records for retried creates (ADR 0004)
- rate-limit counters, and the SSM parameter with the limits (ADR 0006)
"""

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from orders_api.adapters.outbound.dynamodb_orders import (
    BY_CREATED_INDEX,
    BY_OWNER_INDEX,
    LIST_ALL_ATTRIBUTE,
    LIST_SORT_KEY,
    OWNER_ATTRIBUTE,
)
from orders_api.adapters.outbound.dynamodb_rate_limits import EXPIRY_ATTRIBUTE, KEY_ATTRIBUTE
from orders_api.adapters.outbound.ssm_rate_limits import rate_limits_to_json
from orders_api.domain.rate_limits import DEFAULT_RATE_LIMITS
from orders_api_infra.config import EnvConfig

STRING = dynamodb.AttributeType.STRING


class Data(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: EnvConfig) -> None:
        super().__init__(scope, construct_id)

        # Both GSIs exist from the first deploy: Floci can't add one to an existing table.
        self.orders_table = dynamodb.Table(
            self,
            "OrdersTable",
            partition_key=dynamodb.Attribute(name="id", type=STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=config.retains_data
            ),
            deletion_protection=config.retains_data,
            removal_policy=config.data_removal_policy,
        )
        self.orders_table.add_global_secondary_index(
            index_name=BY_OWNER_INDEX,
            partition_key=dynamodb.Attribute(name=OWNER_ATTRIBUTE, type=STRING),
            sort_key=dynamodb.Attribute(name=LIST_SORT_KEY, type=STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.orders_table.add_global_secondary_index(
            index_name=BY_CREATED_INDEX,
            partition_key=dynamodb.Attribute(name=LIST_ALL_ATTRIBUTE, type=STRING),
            sort_key=dynamodb.Attribute(name=LIST_SORT_KEY, type=STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Powertools' default layout: one item per owner-scoped key, removed by TTL after 24 hours.
        # The records are short-lived by design, so no backups and always destroyed (ADR 0005).
        self.idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            partition_key=dynamodb.Attribute(name="id", type=STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiration",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # One short-lived item per scope, subject and window, removed by TTL once the window is
        # over. Counts are worthless afterwards, so no backups and always destroyed (ADR 0006).
        self.rate_limit_table = dynamodb.Table(
            self,
            "RateLimitTable",
            partition_key=dynamodb.Attribute(name=KEY_ATTRIBUTE, type=STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute=EXPIRY_ATTRIBUTE,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=False
            ),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # CDK owns the initial value, operations the current one: a redeploy only overwrites an
        # operator's change when the defaults in code change (ADR 0006).
        self.rate_limits_parameter = ssm.StringParameter(
            self,
            "RateLimitsParameter",
            parameter_name=config.rate_limits_parameter,
            string_value=rate_limits_to_json(DEFAULT_RATE_LIMITS),
            description="Rate limits per IP and per user role, per minute (ADR 0006)",
            tier=ssm.ParameterTier.STANDARD,
        )

        cdk.CfnOutput(
            self, "OrdersTableName", value=self.orders_table.table_name, key="OrdersTableName"
        )
        cdk.CfnOutput(
            self,
            "IdempotencyTableName",
            value=self.idempotency_table.table_name,
            key="IdempotencyTableName",
        )
        cdk.CfnOutput(
            self,
            "RateLimitTableName",
            value=self.rate_limit_table.table_name,
            key="RateLimitTableName",
        )
        cdk.CfnOutput(
            self,
            "RateLimitsParameterName",
            value=self.rate_limits_parameter.parameter_name,
            key="RateLimitsParameterName",
        )
