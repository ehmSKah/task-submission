"""Read the API function's own log lines, to tell which requests actually reached it (ADR 0012)."""

import json
import time
from collections.abc import Iterator
from typing import Any

import boto3
from botocore.exceptions import ClientError

from orders_api_infra.config import load_config

API_FUNCTION_ID_PREFIX = "ApiApiFunction"  # the CDK logical id of Api/ApiFunction


def api_function_name(endpoints: dict[str, Any]) -> str:
    stack = load_config(endpoints["env"]).stack_name
    resources = boto3.client("cloudformation").describe_stack_resources(StackName=stack)
    return next(
        resource["PhysicalResourceId"]
        for resource in resources["StackResources"]
        if resource["ResourceType"] == "AWS::Lambda::Function"
        and resource["LogicalResourceId"].startswith(API_FUNCTION_ID_PREFIX)
    )


class FunctionLogs:
    def __init__(self, function_name: str) -> None:
        configured = boto3.client("lambda").get_function_configuration(FunctionName=function_name)
        # AWS writes to the configured group; Floci always writes to the default one.
        self._groups = {
            configured.get("LoggingConfig", {}).get("LogGroup", ""),
            f"/aws/lambda/{function_name}",
        } - {""}
        self._logs = boto3.client("logs")

    def _messages(self, since_ms: int) -> Iterator[str]:
        for group in self._groups:
            kwargs: dict[str, Any] = {"logGroupName": group, "startTime": since_ms}
            while True:
                try:
                    page = self._logs.filter_log_events(**kwargs)
                except ClientError as error:
                    if error.response["Error"]["Code"] == "ResourceNotFoundException":
                        break
                    raise
                for event in page.get("events", []):
                    yield event.get("message", "")
                if not page.get("nextToken"):
                    break
                kwargs["nextToken"] = page["nextToken"]

    def request_ids(self, since_ms: int) -> set[str]:
        """The `requestId` of every structured line the function wrote since `since_ms`."""
        ids = set()
        for message in self._messages(since_ms):
            try:
                line = json.loads(message)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("requestId"):
                ids.add(str(line["requestId"]))
        return ids

    def wait_for(self, request_id: str, since_ms: int, timeout_s: float = 30) -> set[str]:
        """All logged request ids, once `request_id` is among them (log delivery is async)."""
        deadline = time.monotonic() + timeout_s
        while True:
            ids = self.request_ids(since_ms)
            if request_id in ids:
                return ids
            if time.monotonic() > deadline:
                raise AssertionError(f"request {request_id} never appeared in the function logs")
            time.sleep(1)
