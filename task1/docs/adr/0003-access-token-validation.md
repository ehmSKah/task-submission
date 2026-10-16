---
status: accepted
---

# Access-token validation in a cached Lambda authorizer, built on a provider-agnostic OIDC validator

Access tokens are validated by a **separate REQUEST Lambda authorizer** in front of the API. API Gateway caches its decision per token for **5 minutes**. The validation logic itself is a **standalone module with no Lambda or API Gateway dependency**. It performs standard OpenID Connect checks against a configured issuer, so the same code would run as middleware in a container, and any OIDC provider (Cognito today; Keycloak, Okta or Entra ID tomorrow) is a configuration change. The authorizer answers "who is this?"; the API function answers "may they do this?" ([ADR 0001](0001-role-based-access-control.md)).

## Decision

**Placement**
- **Authorizer type:** a REQUEST Lambda authorizer whose identity source is the `Authorization` header. Requests without that header are rejected by API Gateway with `401` before any function runs.
- **Decision cache:** results are cached per token for **300 seconds**.
- **Policy scope:** the policy it returns allows **the whole API**. The cache is per token, not per route, so a route-specific policy would wrongly deny the token's other routes.
- **Context to the API function:** `sub` and the roles, flattened to a comma-separated string because authorizer context only carries scalar values.
- **No re-verification:** the API function trusts that context. This is safe because its invoke permission allows only this API Gateway to call it.
- **Authorization happens in the API function** (permissions, ownership, `403`/`404`/`409`), as decided in ADR 0001.
- `GET /api/v1/health` has no authorizer.

**Checks**, in order; the first failure rejects the token:
1. **Header:** `Authorization: Bearer <token>` is present and well formed.
2. **Algorithm and key id:** the header's `alg` is on an allow-list (`RS256` only) and `kid` is present. The token's own `alg` is never trusted (prevents `alg: none` / HS256-with-public-key confusion).
3. **Key lookup:** `kid` matches a key in the provider's JWKS.
4. **Signature:** valid.
5. **Issuer:** `iss` equals the configured issuer exactly.
6. **Time:** `exp` is in the future; `iat` and `nbf` (if present) are not in the future, with 30 seconds of clock tolerance.
7. **Token type:** it is an access token, via a configurable required claim (Cognito: `token_use = access`; Keycloak: `typ = Bearer`). The RFC 9068 `typ: at+jwt` header would be the generic marker, but Cognito doesn't set it.
8. **Client:** the calling client is allowed. A configurable claim is checked against an allow-list of client ids (Cognito access tokens carry `client_id` and no `aud`; Keycloak and Entra ID use `azp` / `aud`).
9. **Subject:** `sub` is present; it identifies the order's owner.

Roles are then read from the configured claim and mapped to roles as in ADR 0001. A token with no recognised role still authenticates; the API function denies it with `403`.

**Configuration per identity provider:**
- issuer, plus an optional `jwks_uri` override
- the access-token marker claim and its value
- the client claim and the allowed client ids
- the roles claim and the group → role mapping

**Signing keys (JWKS)**
- **Source:** the key URL comes from OIDC discovery (`{issuer}/.well-known/openid-configuration` → `jwks_uri`) unless the override is set.
- **Cache:** keys live in the authorizer instance's memory with a **1-hour TTL**.
- **Unknown `kid`:** triggers a refetch, **at most once every 60 seconds**, so random `kid`s can't make us hammer the provider. A failed refresh while stale keys exist is also retried at most once every 60 seconds; with no keys at all, every request retries, so the authorizer recovers as soon as the provider does.
- **Fetch timeout:** 2 seconds.
- **Provider unreachable:** cached keys are used even when stale (keys are public and stay valid until rotated). With no keys at all, the authorizer fails closed. API Gateway's `AUTHORIZER_FAILURE` response is mapped to **`503`**.

**Library.** **PyJWT** (with `cryptography`) performs the cryptographic verification and claim checks. The key cache above is our own small component, because PyJWT's `PyJWKClient` has no discovery and no rate limit on refetching.

**Responses.**
- **Every authentication failure** (missing, malformed, invalid or expired token) returns the same **`401`**: the standard error body and `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750). It comes from API Gateway's `UNAUTHORIZED` Gateway Response, whose body is static.
- **The precise reason** is logged by the authorizer with the request id, never returned.
- **`403`** comes only from the API function. The gateway's `ACCESS_DENIED` response is never used.

## Considered options

- **Middleware inside the API function.**
  - *For:* one function, full control over error bodies, simplest to test.
  - *Against:* no gateway-level decision cache, and invalid tokens reach the API function. With the validator as a standalone module, the two placements share all the logic, so this is the fallback if the service moves to a container.
  - *Verdict:* rejected in favour of the cached authorizer and a clean authentication/authorization split.
- **API Gateway's built-in Cognito authorizer.** *Rejected:* it only understands Cognito (against ADR 0001's provider portability), and it would keep the validation the assignment asks for out of our code.
- **Authorizer plus re-verification in the API function.** *Rejected:* double the work for no gain while the API function can only be invoked by this gateway.
- **TOKEN authorizer.** *Rejected:* a REQUEST authorizer can see headers and request context (useful for logging and future checks) at the same cost.
- **Specific `401` reasons** (e.g. "token expired"). Only possible by returning a Deny policy and remapping the gateway's `ACCESS_DENIED` from `403` to `401`. *Rejected:* surprising for maintainers, and it tells attackers which check failed, while a correct client reacts to every `401` the same way (obtain a new token).
- **Validation libraries.**
  - *joserfc:* sound and modern, but less familiar.
  - *Authlib:* a full OAuth framework, too heavy for verifying a JWT.
  - *python-jose:* rejected; barely maintained, with recent CVEs including algorithm confusion (CVE-2024-33663).
  - *Cognito-specific helpers:* rejected; tied to one provider.
  - AWS's `aws-jwt-verify` exists only for JavaScript.
- **JWKS source.** A configured `jwks_uri` alone works but adds a config value per provider. Discovery with an optional override keeps a provider swap to "set the issuer".

## Consequences

- **Expired tokens stay accepted for up to the cache TTL.** On a cache hit, API Gateway doesn't re-check `exp`. A token that expires right after a cached Allow keeps working for up to **5 minutes**. This is the price of the decision cache, bounded by the short TTL.
- **Revocation is not detected.** Local JWT validation can't see Cognito's `RevokeToken` or global sign-out. A revoked access token stays usable for **at most its remaining lifetime plus 5 minutes**. Mitigations: a short access-token lifetime (see ADR 0001) or, if instant revocation becomes a requirement, a provider introspection call or a deny list, which would make the decision cache far more valuable.
- **Scopes are not checked.** Roles carry authorization. Checking client scopes is the next step if individual client applications must be restricted.
- **Two functions in the request path on a cache miss** (authorizer, then API function), each with its own cold start. Cache hits skip the authorizer.
- **A per-IP rate limit can't live in the cached authorizer,** because cache hits skip it. Requests carrying invalid tokens are stopped by the authorizer and never reach the API function. How rate limiting copes with that is decided separately.
- **On Floci:**
  - The issuer and its discovery/JWKS URLs must be reachable from the authorizer's container. This requires `FLOCI_HOSTNAME` (e.g. `http://floci:4566/<pool>`).
  - `cryptography` must match the CPU architecture the Lambda actually runs on.
- **Evolution:** when several services sit behind one gateway, this authorizer becomes their shared authentication point unchanged.
