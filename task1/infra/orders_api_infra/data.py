"""The Data construct: the orders table and its two list indexes (ADR 0005)."""

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from constructs import Construct

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
            index_name="byOwner",
            partition_key=dynamodb.Attribute(name="ownerId", type=STRING),
            sort_key=dynamodb.Attribute(name="createdAtId", type=STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.orders_table.add_global_secondary_index(
            index_name="byCreated",
            partition_key=dynamodb.Attribute(name="listAll", type=STRING),
            sort_key=dynamodb.Attribute(name="createdAtId", type=STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        cdk.CfnOutput(
            self, "OrdersTableName", value=self.orders_table.table_name, key="OrdersTableName"
        )
