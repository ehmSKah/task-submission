---
status: accepted
---

# CORS for browser clients: exact origins per environment, preflights answered by the gateway, static origins where no code runs

A page served from another origin can call the API only if the API answers the browser's CORS checks. The one such page today is Swagger UI on `http://localhost:8080` (`make docs`), whose "Try it out" was rejected for that reason in [ADR 0008](0008-openapi-contract.md). The API now answers CORS **for the exact origins an environment lists, and only those**. An environment that lists none, `aws-prod` and the default, behaves exactly as before: no `Access-Control-*` header, no `Vary`, no `OPTIONS` route.

Every layer that can answer a browser takes part: API Gateway answers the **preflight** itself, the API function adds the headers to **its responses**, and the **Gateway Responses** and **WAF's custom `429`**, which can't run code, carry a static value. The published contract doesn't change.

## Decision

**Allowed origins are per-environment configuration** (`corsAllowedOrigins` in `infra/config/<env>.yaml`, [ADR 0010](0010-infrastructure-and-environments.md)):

| `local` | `aws-dev` | `aws-prod` |
|---|---|---|
| `http://localhost:8080` (Swagger UI) | `http://localhost:8080` | none: CORS off |

- **Validated as browsers send an origin**, so an entry can be compared exactly and never silently fails to match: `http` or `https`, a lower-case host, a port only where it isn't the scheme's default; no wildcard, path, query, user info or trailing slash; no `null`; each origin once. The config model refuses anything else at synth.
- **`aws-dev` allows Swagger UI's origin too.** A developer can point the same local Swagger UI at `aws-dev` (`make docs ENV=aws-dev`), and the integration suite's CORS tests then run on AWS as well, which is how the AWS rows of the verification document get filled in. A page on the developer's own `localhost:8080` gains nothing it couldn't do anyway: it can only call the API with a token it holds itself.
- **`aws-prod` lists none.** There is no production browser client yet, and an origin is added when one exists, with its real `https://` origin. Until then production's synthesised template is byte for byte the same as before this decision; only the function's code bundle gains the CORS module, which does nothing there.

**The preflight is answered by API Gateway** (an `OPTIONS` operation with a MOCK integration on every path, added at synth by `openapi_extensions.py`):
- **`security: []` and no request validator.** A preflight carries no token, so the authorizer never runs; it invokes no function, so the function's rate limits never see it. WAF counts it like any other request.
- **Answer:** `204`, with the security headers, `X-Request-Id` and `Vary: Origin` always. For an allowed origin only, the origin echoed in `Access-Control-Allow-Origin`, the path's own methods in `Access-Control-Allow-Methods` (e.g. `GET, POST` on `/api/v1/orders`), `Authorization, Content-Type, Idempotency-Key, If-Match` in `Access-Control-Allow-Headers`, and `Access-Control-Max-Age: 7200` (Chromium's cap; Firefox allows a day). Any other origin gets no `Access-Control-*` header at all, and the browser stops there.
- **The check is VTL** in the integration response's template: one condition comparing the request's `Origin` with each allowed origin, generated from the config (whose validation keeps an origin from breaking out of its quotes), with every `Access-Control-*` header set inside it.
- **Written for Floci and AWS alike** (both found by a throwaway probe on Floci 2.3.0, see [`verification.md`](../verification.md)):
  - headers are set by index, `$context.responseOverride.header['Access-Control-Allow-Origin']`, because Floci's Velocity can't parse the hyphenated dot form AWS documents
  - the `Origin` header is looked up in any casing by walking `$input.params().header`, because Floci's `$input.params('Origin')` matches the name exactly and HTTP/2 clients send `origin`

**The API function adds the headers to every response it sends**, in the same place as the security headers ([ADR 0012](0012-observability-and-security-baseline.md)), so success, Problem Details and the last-resort `500` all carry them:
- **Allowed origin:** `Access-Control-Allow-Origin` (the origin), `Access-Control-Expose-Headers: ETag, RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset, Retry-After, X-Request-Id`, so a page can use the `ETag` for `If-Match` and see its limits, and `Vary: Origin`.
- **Another origin, or none:** `Vary: Origin` only, since the answer depends on the origin.
- **Configuration:** the allowed origins reach the function as `CORS_ALLOWED_ORIGINS`, set only where there are any; the `Origin` header is matched in any casing.

**Gateway Responses and WAF's `429` carry a static value**, since neither can run code:
- **Exactly one allowed origin** (`local`, `aws-dev`): every Gateway Response (`400` from the validator, `401`, `404`, `413`, `429`, `5xx`, [ADR 0007](0007-validation-and-errors.md)) and WAF's custom `429` ([ADR 0006](0006-rate-limiting.md)) name it, with the same exposed headers and `Vary`. A page from that origin can read why it was refused. A page from any other origin sees a value that isn't its own, so its browser keeps the answer from it, as it would with no header.
- **Several allowed origins:** they carry none, because a static value can't choose. Those pages then see a network error instead of the gateway's `401`, `400` or `429`, while the function's own answers stay readable. That is the trade-off of the edge-optimized endpoint, whose CloudFront distribution can't be configured ([ADR 0002](0002-serverless-rest-api-single-lambda.md)).

**No `Access-Control-Allow-Credentials`, anywhere.** CORS credentials are cookies, HTTP authentication and TLS client certificates, which the browser attaches by itself. A bearer token is a header the page sets itself; allowing `Authorization` in the preflight is all it needs. Never allowing credentials also keeps a mistaken echo harmless: without them, a page can read only what it could fetch with a token it already has.

**The published contract doesn't change.** It lists no `OPTIONS` operation, no `Access-Control-*` header and no `servers`:
- CORS depends on the environment and the contract doesn't: an `OPTIONS` documented for everyone would be a lie in `aws-prod`, which answers it `404` like any undeclared method ([ADR 0007](0007-validation-and-errors.md)).
- A preflight is the browser's business, not an operation a client calls; generated clients and Swagger UI would show it as one.
- **Effect on the tests:** Schemathesis generates requests from the documented operations, so it sends no `OPTIONS`, and the guard that every operation lists exactly the statuses it can return is unchanged. The integration tests send preflights through a plain client, and actual requests with `Origin` through the contract-checked one (undocumented headers are allowed there).

**Swagger UI's "Try it out" works locally.** `make docs` writes `build/swagger/openapi.yaml`, a copy of the contract with the API URL from `build/endpoints.json` as its server, and Swagger UI serves that copy with the submit methods switched on. The committed document keeps no `servers`. The copy names no server when there is no deployed API, or when the environment doesn't allow `http://localhost:8080`. A token from `make token` goes into "Authorize". The docs stay local; nothing is hosted.

## Considered options

- **A same-origin proxy for Swagger UI** (a reverse proxy in Docker Compose serving both the docs and the API). No CORS in the API at all, and nothing to configure per environment. *Rejected:* it helps only that one page, and the point was to see what real CORS costs a browser client.
- **Routing `OPTIONS` to the API function** (Lambda proxy, `security: []`, answered before the rate limits).
  - *For:* one implementation of the origin check, in Python and unit-tested; the proxy integration is already proven on both targets; no VTL dialect to keep in step.
  - *Against:* one invocation per preflight: about $0.22 per million at 256 MB and 5 ms, plus its access log line (about $0.95 per million at 1.5 KB) on top of the request; a cold start can land on a preflight; every preflight fills the function's logs.
  - *Verdict:* rejected once the MOCK integration worked on Floci. It is the fallback if AWS evaluates the template differently.
- **A MOCK with static headers** (what the API Gateway console's "Enable CORS" generates). No VTL, but it can't check the origin: one fixed origin, or `*`, for every caller. *Rejected:* a preflight from any origin would be answered as if allowed.
- **`Access-Control-Allow-Origin: *`.** Without credentials it is not unsafe for a bearer-token API, and it needs no configuration. *Rejected:* it opens the API to every site's pages when the clients are known, and it can't be combined with credentials should cookies ever come.
- **Gateway Responses echoing `method.request.header.Origin`.** Works with any number of origins, and Floci renders it. *Rejected:* it reflects every origin unchecked, so the allow-list would be false at one layer. Harmless today (no credentials, and gateway errors hold nothing but the request id and the validator's message about the page's own request), but it is the reflected-origin pattern security scanners flag, and a real hole the day credentials are allowed.
- **Powertools' `CORSConfig`** in the function's router. It answers preflights and adds headers, but preflights would invoke the function, and the last-resort `500` bypasses the router. The headers belong where the security headers are added.
- **Listing `OPTIONS` in the published contract.** *Rejected,* see above.
- **Wildcard or pattern origins** (`https://*.example.com`). *Rejected:* an exact list is what can be reviewed, and every match is a string comparison in VTL and in Python.
- **A regional endpoint with our own CloudFront and a response headers policy.** CloudFront's CORS configuration takes a list of origins and applies to every response, errors included, without code. *The evolution* once several browser origins must read gateway errors; it comes with the regional endpoint ADR 0002 already names for caching or edge WAF.

## Consequences

- **Clients that send no `Origin` see no change.** In `local` and `aws-dev` their responses gain `Vary: Origin` and, from the gateway's error responses and WAF, the static origin headers, which a client that isn't a browser ignores. In `aws-prod` nothing changes at all.
- **A browser client costs up to one extra request per call.** Every call except `GET /api/v1/health` sends `Authorization`, which makes it a non-simple request, so the browser sends a preflight first. Browsers cache the answer per origin and URL for up to `Access-Control-Max-Age` (two hours in Chromium), but every order id and every list cursor is a new URL. A preflight costs what a request costs at the gateway and WAF, about $4.30 per million (section 8 of the design document), and nothing in Lambda or DynamoDB. A page pays one extra round trip to the edge before the first call to each URL.
- **WAF's per-IP flood rule counts preflights**, so a browser's calls can count up to twice towards it. The function's limits never see them.
- **Adding or removing an origin is a deploy**: it changes the gateway definition and the function's environment.
- **Several origins make gateway errors unreadable** for their pages (see above), until the evolution above.
- **Two implementations of the origin check**, VTL in the preflight and Python in the function, both generated from the one config list. The integration suite sends an allowed and a refused origin to both.
- **To verify on AWS:** that API Gateway evaluates the same template (the index form, walking the headers, an empty `204` body), that the edge-optimized endpoint passes `Origin` and the `Access-Control-*` headers through, and that WAF accepts the CORS headers in its custom response. The integration suite's CORS tests run on `aws-dev` unchanged.
