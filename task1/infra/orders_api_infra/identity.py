"""The Identity construct: Cognito user pool, public app client, role groups (ADRs 0001, 0010).

It also owns what the authorizer must know about this identity provider (ADR 0003) and, where
the environment sets `testFixtures`, the test-only Cognito resources (ADR 0011).
"""

import aws_cdk as cdk
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from orders_api.adapters.inbound.authorizer_handler import environment_for
from orders_api.auth.tokens import ValidatorConfig
from orders_api.domain.access import Role
from orders_api_infra.config import EnvConfig

APP_CLIENT_NAME = "orders-api-client"
UNTRUSTED_CLIENT_NAME = "orders-api-untrusted-client"
SHORT_LIVED_CLIENT_NAME = "orders-api-short-lived-client"
FOREIGN_CLIENT_NAME = "orders-api-foreign-client"
TOKEN_VALIDITY_MINUTES = 15
"""How long the app client's access and ID tokens last (ADR 0010)."""

# The test-only resources' stack outputs, and their keys in build/endpoints.json (ADR 0011).
FIXTURE_OUTPUTS = {
    "FixtureForeignUserPoolId": "fixtureForeignUserPoolId",
    "FixtureForeignClientId": "fixtureForeignClientId",
    "FixtureUntrustedClientId": "fixtureUntrustedClientId",
    "FixtureShortLivedClientId": "fixtureShortLivedClientId",
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
            # Where data is kept, the users are too: the pool can't be deleted by accident.
            deletion_protection=config.retains_data,
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
            access_token_validity=cdk.Duration.minutes(TOKEN_VALIDITY_MINUTES),
            id_token_validity=cdk.Duration.minutes(TOKEN_VALIDITY_MINUTES),
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

        self.fixtures = (
            TestFixtures(self, "TestFixtures", config=config, user_pool=self.user_pool)
            if config.test_fixtures
            else None
        )
        allowed_clients = [self.client.user_pool_client_id]
        if self.fixtures is not None:
            # Trusted, so that its expired tokens are rejected for their time alone (ADR 0011).
            allowed_clients.append(self.fixtures.short_lived_client.user_pool_client_id)
        # Cognito's access tokens, described for the provider-agnostic validator (ADR 0003).
        # The groups are named after the roles, so no group -> role mapping is needed (ADR 0001).
        self.authorizer_environment = environment_for(
            ValidatorConfig(
                issuer=self.issuer,
                token_type_claim="token_use",
                token_type_value="access",
                client_claim="client_id",
                allowed_clients=frozenset(allowed_clients),
                roles_claim="cognito:groups",
            ),
            everyone_is_user=config.everyone_is_user,
        )

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


class TestFixtures(Construct):
    """Test-only Cognito resources for the invalid-token tests (ADR 0011).

    - a foreign user pool with its own client: real tokens with another issuer and key
    - an untrusted client in our pool: real tokens whose client is not on the allow-list
    - a short-lived client in our pool: real tokens that expire within seconds on Floci
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvConfig,
        user_pool: cognito.UserPool,
    ) -> None:
        super().__init__(scope, construct_id)
        self.foreign_user_pool = cognito.UserPool(
            self,
            "ForeignUserPool",
            user_pool_name=f"orders-api-{config.name}-foreign",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            account_recovery=cognito.AccountRecovery.NONE,
            password_policy=_password_policy(),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.foreign_client = self._client(
            self.foreign_user_pool, "ForeignClient", FOREIGN_CLIENT_NAME
        )
        self.untrusted_client = self._client(user_pool, "UntrustedClient", UNTRUSTED_CLIENT_NAME)
        self.short_lived_client = self._client(
            user_pool, "ShortLivedClient", SHORT_LIVED_CLIENT_NAME
        )
        # CDK refuses lifetimes below AWS's 5-minute minimum, so they go on the L1 resource.
        short_lived = self.short_lived_client.node.default_child
        assert isinstance(short_lived, cognito.CfnUserPoolClient)
        short_lived.access_token_validity = config.fixture_token_validity_seconds
        short_lived.id_token_validity = config.fixture_token_validity_seconds
        short_lived.refresh_token_validity = 1
        short_lived.token_validity_units = cognito.CfnUserPoolClient.TokenValidityUnitsProperty(
            access_token="seconds", id_token="seconds", refresh_token="days"
        )

        values = {
            "FixtureForeignUserPoolId": self.foreign_user_pool.user_pool_id,
            "FixtureForeignClientId": self.foreign_client.user_pool_client_id,
            "FixtureUntrustedClientId": self.untrusted_client.user_pool_client_id,
            "FixtureShortLivedClientId": self.short_lived_client.user_pool_client_id,
        }
        for output in FIXTURE_OUTPUTS:
            cdk.CfnOutput(self, output, value=values[output], key=output)

    def _client(
        self, user_pool: cognito.UserPool, construct_id: str, name: str
    ) -> cognito.UserPoolClient:
        return cognito.UserPoolClient(
            self,
            construct_id,
            user_pool=user_pool,
            user_pool_client_name=name,
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            prevent_user_existence_errors=True,
        )
