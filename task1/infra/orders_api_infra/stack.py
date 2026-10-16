"""The single OrdersApi stack (ADR 0010).

The Identity and Data constructs join the Api construct as their resources are built.
"""

import aws_cdk as cdk
from constructs import Construct

from orders_api_infra.api import Api
from orders_api_infra.config import EnvConfig


class OrdersApiStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: EnvConfig) -> None:
        super().__init__(
            scope,
            construct_id,
            env=cdk.Environment(account=config.account, region=config.region),
            termination_protection=config.retains_data,
        )
        self.api = Api(self, "Api", config=config)
