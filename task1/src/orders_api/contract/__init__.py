"""The OpenAPI document as the API function sees it: the single source of request rules.

The function validates against the same schemas the gateway was built from (ADRs 0007-0009). In
the Lambda bundle `openapi.yaml` sits next to this module; in a source checkout it is read from
`openapi/`.
"""

from functools import cache
from pathlib import Path
from typing import Any

import yaml

DRAFT_04 = "http://json-schema.org/draft-04/schema#"
SPEC_FILE = "openapi.yaml"
JSON_MEDIA_TYPE = "application/json"


def _spec_path() -> Path:
    bundled = Path(__file__).with_name(SPEC_FILE)
    if bundled.is_file():
        return bundled
    return Path(__file__).parents[3] / "openapi" / SPEC_FILE


@cache
def load_contract() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(_spec_path().read_text())
    return spec


def _draft_04(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` as a standalone draft-04 document whose `#/components/schemas/…` refs resolve."""
    return {
        "$schema": DRAFT_04,
        **schema,
        "components": {"schemas": load_contract()["components"]["schemas"]},
    }


@cache
def request_body_schema(method: str, path: str) -> dict[str, Any]:
    """The JSON request body schema of one operation; `KeyError` if it has none."""
    operation = load_contract()["paths"][path][method]
    schema: dict[str, Any] = operation["requestBody"]["content"][JSON_MEDIA_TYPE]["schema"]
    return _draft_04(schema)


@cache
def path_parameter_schema(path: str, name: str) -> dict[str, Any]:
    """The schema of a path-level parameter; `KeyError` if the path doesn't declare it."""
    for parameter in load_contract()["paths"][path].get("parameters", []):
        if parameter["in"] == "path" and parameter["name"] == name:
            return _draft_04(parameter["schema"])
    raise KeyError(f"{path} declares no path parameter {name!r}")
