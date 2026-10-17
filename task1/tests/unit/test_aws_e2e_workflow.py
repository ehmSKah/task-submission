"""The aws-e2e workflow's promises (ADR 0011): manual only, OIDC rather than keys, one run at a
time, and the stack destroyed whatever happened before. `make lint-workflows` checks the rest
of the file with actionlint."""

from typing import Any

import pytest
import yaml

from orders_api_infra.paths import PROJECT_DIR

WORKFLOW = PROJECT_DIR.parent / ".github" / "workflows" / "aws-e2e.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return loaded


@pytest.fixture(scope="module")
def steps(workflow: dict[Any, Any]) -> list[dict[str, Any]]:
    [job] = workflow["jobs"].values()
    found: list[dict[str, Any]] = job["steps"]
    return found


def test_it_runs_only_when_triggered_by_hand(workflow: dict[Any, Any]) -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    assert list(workflow[True]) == ["workflow_dispatch"]


def test_it_gets_credentials_through_oidc_for_the_role_in_a_repository_variable(
    workflow: dict[Any, Any], steps: list[dict[str, Any]]
) -> None:
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    logins = [step for step in steps if "configure-aws-credentials" in step.get("uses", "")]
    assert logins
    for login in logins:
        assert login["with"]["role-to-assume"] == "${{ vars.AWS_E2E_ROLE_ARN }}"
        assert not any("secret" in key or "access-key" in key for key in login["with"])
    assert "secrets." not in WORKFLOW.read_text()


def test_two_runs_never_overlap(workflow: dict[Any, Any]) -> None:
    assert workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_the_stack_is_destroyed_with_fresh_credentials_even_when_tests_fail(
    steps: list[dict[str, Any]],
) -> None:
    destroy = steps[-1]
    login = steps[-2]

    assert destroy["run"] == "make down ENV=aws-dev"
    assert destroy["if"] == "always()"
    assert login["if"] == "always()"
    assert "configure-aws-credentials" in login["uses"]
    deploy = next(step for step in steps if "make deploy" in step.get("run", ""))
    assert steps.index(deploy) < steps.index(destroy)


def test_it_runs_the_integration_and_contract_suites_against_aws_dev(
    steps: list[dict[str, Any]],
) -> None:
    commands = [step.get("run", "") for step in steps]

    assert any(c.startswith("make test-integration ENV=aws-dev") for c in commands)
    assert any(c.startswith("make contract-test ENV=aws-dev") for c in commands)
    assert "make deploy endpoints seed ENV=aws-dev" in commands
