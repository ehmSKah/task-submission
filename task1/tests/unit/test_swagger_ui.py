"""`make docs`: Swagger UI's copy of the contract, pointed at the deployed API so "Try it out"
works, while the committed document names no server (ADRs 0008 and 0014)."""

import json
from pathlib import Path

import pytest
import yaml

from orders_api_infra import swagger_ui
from orders_api_infra.config import load_config
from orders_api_infra.openapi_extensions import load_spec
from orders_api_infra.paths import PROJECT_DIR, SPEC_PATH
from orders_api_infra.swagger_ui import SWAGGER_UI_ORIGIN, served_spec, write

API_URL = "http://localhost:4566/restapis/abc123/live/_user_request_"


def test_the_served_copy_names_the_deployed_api_as_its_server() -> None:
    served = served_spec(load_config("local"), API_URL)

    assert served["servers"] == [{"url": API_URL}]
    assert {key: value for key, value in served.items() if key != "servers"} == load_spec()


def test_the_committed_contract_names_no_server() -> None:
    """The URL depends on the deployment (ADR 0008)."""
    assert "servers" not in yaml.safe_load(SPEC_PATH.read_text())


def test_without_a_deployed_api_the_copy_names_no_server() -> None:
    assert "servers" not in served_spec(load_config("local"), None)


def test_an_environment_that_doesn_t_allow_swagger_ui_gets_no_server() -> None:
    """aws-prod allows no origin: the browser would block every call, so nothing is offered."""
    served = served_spec(load_config("aws-prod"), "https://api.example.test/live")

    assert "servers" not in served


@pytest.mark.parametrize("env", ["local", "aws-dev"])
def test_swagger_ui_s_origin_is_allowed_where_it_may_call_the_api(env: str) -> None:
    assert SWAGGER_UI_ORIGIN in load_config(env).cors_allowed_origins


def test_swagger_ui_is_served_on_its_allowed_origin_and_may_send_requests() -> None:
    compose = yaml.safe_load((PROJECT_DIR / "docker-compose.yml").read_text())
    service = compose["services"]["swagger-ui"]

    assert service["ports"] == [f"{SWAGGER_UI_ORIGIN.rsplit(':', 1)[1]}:8080"]
    assert json.loads(service["environment"]["SUPPORTED_SUBMIT_METHODS"]) == [
        "get",
        "post",
        "patch",
        "delete",
    ]
    assert service["environment"]["SWAGGER_JSON"] == "/spec/openapi.yaml"
    assert service["volumes"] == [f"./{swagger_ui.SERVED_DIR.relative_to(PROJECT_DIR)}:/spec:ro"]


def test_the_copy_is_written_where_swagger_ui_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "swagger" / "openapi.yaml"

    write(path, served_spec(load_config("local"), API_URL))

    assert yaml.safe_load(path.read_text())["servers"] == [{"url": API_URL}]
