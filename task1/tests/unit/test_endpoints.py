"""`build/endpoints.json`: the one file that says where the deployed API is (ADR 0011)."""

import json
from pathlib import Path

import pytest

from orders_api_infra.endpoints import read


def written_for(tmp_path: Path, env: str) -> Path:
    path = tmp_path / "endpoints.json"
    path.write_text(json.dumps({"env": env, "apiUrl": f"https://{env}.example.test/live"}))
    return path


def test_the_file_is_read_for_the_environment_it_was_written_for(tmp_path: Path) -> None:
    path = written_for(tmp_path, "local")

    assert read("local", path=path)["apiUrl"] == "https://local.example.test/live"


def test_any_environment_is_accepted_when_none_is_asked_for(tmp_path: Path) -> None:
    assert read(path=written_for(tmp_path, "aws-dev"))["env"] == "aws-dev"


def test_a_file_written_for_another_environment_is_refused(tmp_path: Path) -> None:
    """Otherwise one environment's tokens would go to the other's API."""
    path = written_for(tmp_path, "local")

    with pytest.raises(SystemExit, match=r"'local'.*ENV=aws-dev"):
        read("aws-dev", path=path)


def test_a_missing_file_says_how_to_write_it(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="make up"):
        read("local", path=tmp_path / "endpoints.json")
