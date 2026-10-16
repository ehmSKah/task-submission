"""Every integration-test response is checked against the OpenAPI document (ADRs 0008 and 0011).

`ContractCheck` is installed as a response hook on the shared `api` client, so no test can skip
it. Undocumented status codes, missing required headers and bodies that don't match their schema
(including undocumented fields) fail the test that made the request.
"""

import copy
import json
from typing import Any
from urllib.parse import parse_qs

import httpx
from openapi_core import Config, OpenAPI
from openapi_core.datatypes import RequestParameters
from openapi_core.deserializing.media_types.util import json_loads
from openapi_core.templating.paths.exceptions import OperationNotFound, PathNotFound
from openapi_schema_validator import OAS30Validator
from werkzeug.datastructures import Headers, ImmutableMultiDict

from orders_api.adapters.inbound.http_headers import PROBLEM_JSON_MEDIA_TYPE
from orders_api_infra.openapi_extensions import load_spec


class _Request:
    """An httpx request in the shape openapi-core reads."""

    def __init__(self, request: httpx.Request) -> None:
        self._request = request
        self.parameters = RequestParameters(
            query=ImmutableMultiDict(parse_qs(request.url.query.decode())),
            header=Headers(dict(request.headers)),
            cookie=ImmutableMultiDict(),
        )

    @property
    def host_url(self) -> str:
        return f"{self._request.url.scheme}://{self._request.url.netloc.decode()}"

    @property
    def path(self) -> str:
        return self._request.url.path

    @property
    def method(self) -> str:
        return self._request.method.lower()

    @property
    def body(self) -> bytes | None:
        return self._request.content or None

    @property
    def content_type(self) -> str:
        return self._request.headers.get("Content-Type", "").lower()


class _Response:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def content_type(self) -> str:
        return self._response.headers.get("Content-Type", "").lower()

    @property
    def headers(self) -> Headers:
        return Headers(dict(self._response.headers))

    @property
    def data(self) -> bytes:
        return self._response.content


class ContractCheck:
    def __init__(self, api_url: str) -> None:
        spec = copy.deepcopy(load_spec())
        spec["servers"] = [{"url": api_url}]
        self._spec = spec
        self._openapi = OpenAPI.from_dict(
            spec,
            config=Config(extra_media_type_deserializers={PROBLEM_JSON_MEDIA_TYPE: json_loads}),
        )

    def __call__(self, response: httpx.Response) -> None:
        """An httpx response hook: the body is read first so the test can still use it."""
        response.read()
        self.check(response)

    def check(self, response: httpx.Response) -> None:
        try:
            self._openapi.validate_response(_Request(response.request), _Response(response))
        except (PathNotFound, OperationNotFound):
            self._check_unknown_route(response)

    def _check_unknown_route(self, response: httpx.Response) -> None:
        """No operation to match against: the documented answer is the `NotFound` problem."""
        assert response.status_code == 404, (
            f"{response.request.method} {response.request.url.path} is not in the contract, "
            f"yet answered {response.status_code}"
        )
        documented = self._spec["components"]["responses"]["NotFound"]["content"]
        assert response.headers["Content-Type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
        schema = {
            **documented[PROBLEM_JSON_MEDIA_TYPE]["schema"],
            "components": self._spec["components"],
        }
        OAS30Validator(schema).validate(json.loads(response.content))


def contract_checked_client(api_url: str, **kwargs: Any) -> httpx.Client:
    return httpx.Client(
        base_url=api_url, event_hooks={"response": [ContractCheck(api_url)]}, **kwargs
    )
