"""The Identity construct: Cognito user pool, public app client, role groups (ADRs 0001, 0010).

It also owns what the authorizer must know about this identity provider (ADR 0003).
"""

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from orders_api.domain.access import Role
from orders_api_infra.config import EnvConfig

APP_CLIENT_NAME = "orders-api-client"

# Cognito's access tokens, described for the provider-agnostic validator (ADR 0003). The groups
# are named after the roles, so no group -> role mapping is needed (ADR 0001).
COGNITO_TOKEN_PROFILE = {
    "TOKEN_TYPE_CLAIM": "token_use",
    "TOKEN_TYPE_VALUE": "access",
    "TOKEN_CLIENT_CLAIM": "client_id",
    "TOKEN_ROLES_CLAIM": "cognito:groups",
}


def _password_policy() -> cognito.PasswordPolicy:
    return cognito.PasswordPolicy(
        min_length=12,
        require_lowercase=True,
        require_uppercase=True,
        require_digits=True,
        require_symbols=True,
    )


class Identity(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, config: EnvConfig) -> None:
        super().__init__(scope, construct_id)
        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"orders-api-{config.name}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            account_recovery=cognito.AccountRecovery.NONE,
            password_policy=_password_policy(),
            removal_policy=config.data_removal_policy,
        )
        if config.floci_user_pool_id:
            # Stable ids on Floci, so the issuer and client id survive redeploys (ADR 0010).
            cdk.Tags.of(self.user_pool).add("floci:override-id", config.floci_user_pool_id)
            cdk.Tags.of(self.user_pool).add("floci:override-cognito-client-id", "use-name")

        # Public client: the API never authenticates as the client, so no secret (ADR 0010).
        self.client = self.user_pool.add_client(
            "AppClient",
            user_pool_client_name=APP_CLIENT_NAME,
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            access_token_validity=cdk.Duration.minutes(15),
            id_token_validity=cdk.Duration.minutes(15),
            refresh_token_validity=cdk.Duration.days(1),
            prevent_user_existence_errors=True,
        )

        for role in Role:
            cognito.CfnUserPoolGroup(
                self,
                f"Group{role.value.title().replace('_', '')}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=role.value,
                description=f"Members hold the {role.value} role in the Orders API",
            )

        self.issuer = (
            f"{config.floci_issuer_base_url}/{self.user_pool.user_pool_id}"
            if config.floci_issuer_base_url
            else self.user_pool.user_pool_provider_url
        )

        # The authorizer's provider settings, as its environment (ADR 0003).
        self.authorizer_environment: dict[str, str] = {
            "TOKEN_ISSUER": self.issuer,
            "TOKEN_ALLOWED_CLIENTS": self.client.user_pool_client_id,
            **COGNITO_TOKEN_PROFILE,
        }

        self.seed_users_secret: secretsmanager.Secret | None = None
        if config.seed_users:
            # One generated password shared by the demo users; never stored in the repo.
            self.seed_users_secret = secretsmanager.Secret(
                self,
                "SeedUsersSecret",
                secret_name=f"/orders-api/{config.name}/seed-users",
                description="Password of the seeded demo users (ADR 0010)",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template="{}",
                    generate_string_key="password",
                    password_length=24,
                    exclude_characters="\"'\\/@`$ ",
                    require_each_included_type=True,
                ),
                removal_policy=cdk.RemovalPolicy.DESTROY,
            )
            cdk.CfnOutput(
                self,
                "SeedUsersSecretName",
                value=self.seed_users_secret.secret_name,
                key="SeedUsersSecretName",
            )

        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id, key="UserPoolId")
        cdk.CfnOutput(
            self, "UserPoolClientId", value=self.client.user_pool_client_id, key="UserPoolClientId"
        )
        cdk.CfnOutput(self, "TokenIssuer", value=self.issuer, key="TokenIssuer")
