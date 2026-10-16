"""Write `build/endpoints.json`: the one file tests and humans read to find the API (ADR 0011).

The REST API id changes on every deploy, so the URL is derived from the stack outputs.

Usage: python -m orders_api_infra.endpoints --env local --floci-url http://localhost:4566
"""

import argparse
import json

from orders_api_infra.config import EnvConfig, load_config
from orders_api_infra.identity import FIXTURE_OUTPUTS
from orders_api_infra.paths import BUILD_DIR


def api_base_url(config: EnvConfig, outputs: dict[str, str], floci_url: str) -> str:
    api_id, stage = outputs["ApiId"], outputs["StageName"]
    if config.target == "floci":
        return f"{floci_url.rstrip('/')}/restapis/{api_id}/{stage}/_user_request_"
    return f"https://{api_id}.execute-api.{config.region}.amazonaws.com/{stage}"


def stack_outputs(config: EnvConfig) -> dict[str, str]:
    """The outputs `cdk deploy --outputs-file` wrote for this environment's stack."""
    outputs_by_stack = json.loads((BUILD_DIR / "cdk-outputs.json").read_text())
    outputs: dict[str, str] = outputs_by_stack[config.stack_name]
    return outputs


def write(env: str, floci_url: str) -> dict[str, str]:
    config = load_config(env)
    outputs = stack_outputs(config)
    endpoints = {
        "env": config.name,
        "target": config.target,
        "region": config.region,
        "apiUrl": api_base_url(config, outputs, floci_url),
        "userPoolId": outputs["UserPoolId"],
        "userPoolClientId": outputs["UserPoolClientId"],
        "tokenIssuer": outputs["TokenIssuer"],
        "ordersTable": outputs["OrdersTableName"],
    }
    if "SeedUsersSecretName" in outputs:
        endpoints["seedUsersSecret"] = outputs["SeedUsersSecretName"]
    # Present only where the environment sets testFixtures (ADR 0011).
    for output, key in FIXTURE_OUTPUTS.items():
        if output in outputs:
            endpoints[key] = outputs[output]
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
