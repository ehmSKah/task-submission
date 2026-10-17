"""Write the copy of the OpenAPI document that Swagger UI serves (`make docs`).

The committed contract names no server: the URL depends on the deployment (ADR 0008). The copy
adds the API URL from `build/endpoints.json` as its server, so "Try it out" calls the deployed
API from the browser, which CORS allows for Swagger UI's origin (ADR 0014). Without a deployed
API, or for an environment that doesn't allow that origin, the copy names no server and the
docs are only for reading.

Usage: python -m orders_api_infra.swagger_ui --env local
"""

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from orders_api_infra import endpoints
from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.openapi_extensions import load_spec
from orders_api_infra.paths import BUILD_DIR

# Where `make docs` serves Swagger UI (docker-compose.yml).
SWAGGER_UI_ORIGIN = "http://localhost:8080"
SERVED_DIR = BUILD_DIR / "swagger"


def served_spec(config: EnvConfig, api_url: str | None) -> dict[str, Any]:
    """The contract, with `api_url` as its server where Swagger UI may call it."""
    spec = load_spec()
    if api_url is not None and SWAGGER_UI_ORIGIN in config.cors_allowed_origins:
        spec["servers"] = [{"url": api_url}]
    return spec


def write(path: Path, spec: dict[str, Any]) -> None:
    """Replace the file's content in place: Swagger UI's container links to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))


def _deployed_api_url(config: EnvConfig) -> str | None:
    if not endpoints.ENDPOINTS_FILE.is_file():
        return None
    api_url: str = endpoints.read(config.name)["apiUrl"]
    return api_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    config = load_config(parser.parse_args().env)
    spec = served_spec(config, _deployed_api_url(config))
    write(SERVED_DIR / "openapi.yaml", spec)
    if "servers" in spec:
        print(
            f'"Try it out" calls {json.dumps(spec["servers"][0]["url"])}: paste a token from '
            f"`make token ENV={config.name}` into Authorize."
        )
    elif SWAGGER_UI_ORIGIN not in config.cors_allowed_origins:
        print(f"{config.name} doesn't allow {SWAGGER_UI_ORIGIN}: the docs are only for reading.")
    else:
        print('No deployed API (build/endpoints.json): run `make up` for "Try it out".')


if __name__ == "__main__":
    main()
