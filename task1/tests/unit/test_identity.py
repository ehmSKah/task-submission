"""The Identity construct: the Cognito profile for the authorizer, and the test fixtures."""

from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.identity import Identity


def config(**overrides: Any) -> EnvConfig:
    return EnvConfig.model_validate({**load_config("local").model_dump(by_alias=True), **overrides})


def synth(env_config: EnvConfig) -> tuple[cdk.Stack, Identity, Template]:
    stack = cdk.Stack(cdk.App(), "IdentityUnderTest")
    identity = Identity(stack, "Identity", config=env_config)
    return stack, identity, Template.from_stack(stack)


def test_test_fixtures_are_off_unless_an_environment_sets_them() -> None:
    settings = load_config("local").model_dump(by_alias=True)
    del settings["testFixtures"]
    del settings["fixtureTokenValiditySeconds"]

    unset = EnvConfig.model_validate(settings)

    assert unset.test_fixtures is False
    assert unset.fixture_token_validity_seconds == 300  # AWS's minimum lifetime


def test_local_sets_up_the_test_fixtures_with_second_long_tokens() -> None:
    local = load_config("local")

    assert local.test_fixtures is True
    assert local.fixture_token_validity_seconds == 1


@pytest.fixture(scope="module")
def without_fixtures() -> tuple[cdk.Stack, Identity, Template]:
    return synth(config(testFixtures=False))


@pytest.fixture(scope="module")
def with_fixtures() -> tuple[cdk.Stack, Identity, Template]:
    return synth(config(testFixtures=True, fixtureTokenValiditySeconds=3))


def test_fixtures_are_absent_when_test_fixtures_is_false(
    without_fixtures: tuple[cdk.Stack, Identity, Template],
) -> None:
    stack, identity, template = without_fixtures

    template.resource_count_is("AWS::Cognito::UserPool", 1)
    template.resource_count_is("AWS::Cognito::UserPoolClient", 1)
    assert not any(name.startswith("Fixture") for name in template.find_outputs("*"))
    assert stack.resolve(identity.authorizer_environment["TOKEN_ALLOWED_CLIENTS"]) == {
        "Ref": stack.get_logical_id(identity.client.node.default_child)  # type: ignore[arg-type]
    }


def test_fixtures_are_a_foreign_pool_an_untrusted_and_a_short_lived_client(
    with_fixtures: tuple[cdk.Stack, Identity, Template],
) -> None:
    _, _, template = with_fixtures

    template.resource_count_is("AWS::Cognito::UserPool", 2)
    template.resource_count_is("AWS::Cognito::UserPoolClient", 4)
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "ClientName": "orders-api-short-lived-client",
            "AccessTokenValidity": 3,
            "TokenValidityUnits": Match.object_like({"AccessToken": "seconds"}),
        },
    )
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient", {"ClientName": "orders-api-untrusted-client"}
    )
    outputs = template.find_outputs("*")
    for name in (
        "FixtureForeignUserPoolId",
        "FixtureForeignClientId",
        "FixtureUntrustedClientId",
        "FixtureShortLivedClientId",
    ):
        assert name in outputs


def test_only_the_app_client_and_the_short_lived_client_are_allowed(
    with_fixtures: tuple[cdk.Stack, Identity, Template],
) -> None:
    stack, identity, _ = with_fixtures
    assert identity.fixtures is not None

    allowed = stack.resolve(identity.authorizer_environment["TOKEN_ALLOWED_CLIENTS"])

    refs = [part["Ref"] for part in allowed["Fn::Join"][1]]
    assert sorted(refs) == sorted(
        stack.get_logical_id(client.node.default_child)  # type: ignore[arg-type]
        for client in (identity.client, identity.fixtures.short_lived_client)
    )


def test_the_authorizer_gets_the_cognito_profile(
    without_fixtures: tuple[cdk.Stack, Identity, Template],
) -> None:
    stack, identity, _ = without_fixtures
    environment = identity.authorizer_environment

    assert environment["TOKEN_TYPE_CLAIM"] == "token_use"
    assert environment["TOKEN_TYPE_VALUE"] == "access"
    assert environment["TOKEN_CLIENT_CLAIM"] == "client_id"
    assert environment["TOKEN_ROLES_CLAIM"] == "cognito:groups"
    assert "TOKEN_GROUP_ROLES" not in environment  # the groups are named after the roles
    issuer = stack.resolve(environment["TOKEN_ISSUER"])
    assert issuer["Fn::Join"][1][0] == "http://floci:4566/"
