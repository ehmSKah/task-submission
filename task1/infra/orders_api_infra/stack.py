"""The single OrdersApi stack, built from three constructs (ADR 0010)."""

import aws_cdk as cdk
from constructs import Construct

from orders_api_infra.api import Api
from orders_api_infra.config import EnvConfig
from orders_api_infra.data import Data
from orders_api_infra.identity import Identity


class OrdersApiStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: EnvConfig) -> None:
        super().__init__(
            scope,
            construct_id,
            env=cdk.Environment(account=config.account, region=config.region),
            termination_protection=config.retains_data,
        )
        self.identity = Identity(self, "Identity", config=config)
        self.data = Data(self, "Data", config=config)
        self.api = Api(
            self,
            "Api",
            config=config,
            authorizer_environment=self.identity.authorizer_environment,
            orders_table=self.data.orders_table,
            idempotency_table=self.data.idempotency_table,
        )
