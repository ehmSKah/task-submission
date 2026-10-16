"""Write `build/endpoints.json`: the one file tests and humans read to find the API (ADR 0011).

The REST API id changes on every deploy, so the URL is derived from the stack outputs.

Usage: python -m orders_api_infra.endpoints --env local --floci-url http://localhost:4566
"""

import argparse
import json

from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.paths import BUILD_DIR


def api_base_url(config: EnvConfig, outputs: dict[str, str], floci_url: str) -> str:
    api_id, stage = outputs["ApiId"], outputs["StageName"]
    if config.target == "floci":
        return f"{floci_url.rstrip('/')}/restapis/{api_id}/{stage}/_user_request_"
    return f"https://{api_id}.execute-api.{config.region}.amazonaws.com/{stage}"


def write(env: str, floci_url: str) -> dict[str, str]:
    config = load_config(env)
    outputs_by_stack = json.loads((BUILD_DIR / "cdk-outputs.json").read_text())
    outputs: dict[str, str] = outputs_by_stack[config.stack_name]
    endpoints = {
        "env": config.name,
        "target": config.target,
        "region": config.region,
        "apiUrl": api_base_url(config, outputs, floci_url),
    }
    (BUILD_DIR / "endpoints.json").write_text(json.dumps(endpoints, indent=2) + "\n")
    return endpoints


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="local")
    parser.add_argument("--floci-url", default="http://localhost:4566")
    args = parser.parse_args()
    print(f"API: {write(args.env, args.floci_url)['apiUrl']}")


if __name__ == "__main__":
    main()
