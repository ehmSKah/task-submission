---
status: accepted
---

# Role-based access control: identity-provider groups mapped to roles and permissions

Roles (`USER`, `ORDER_MANAGER`, `ADMIN`) come from the identity provider's **groups**, delivered in the access token (`cognito:groups` in Amazon Cognito). The API translates groups into roles and roles into **permissions** through configuration, and every access check asks about a permission, never about a role or a group. That keeps the authorization model independent of Cognito: switching to another OpenID Connect provider (Keycloak, Okta, Microsoft Entra ID) changes configuration, not handlers.

## Decision

**Permission matrix.** Roles are nested; ADMIN has every permission.

| Permission | USER | ORDER_MANAGER | ADMIN |
|---|:-:|:-:|:-:|
| `order:create` (always for yourself) | ✅ | ✅ | ✅ |
| `order:read:own` | ✅ | ✅ | ✅ |
| `order:read:any` | | ✅ | ✅ |
| `order:cancel:own` (status change to CANCELLED only) | ✅ | ✅ | ✅ |
| `order:status:any` (any allowed status change) | | ✅ | ✅ |
| `order:delete:any` | | | ✅ |

`GET /api/v1/health` is public and needs no token.

**Cancellation vs deletion.** Cancelling is a business action a customer may take on their own order. Deleting removes the record and is an administrative act reserved for ADMIN, so customers can't erase their order history.

**Several roles, or none.** A person's permissions are the union of the permissions of all their recognised roles. Groups the API doesn't know are ignored. A valid token with no recognised role is denied (`403` on every order endpoint). Mapping every authenticated person to `USER` (for self-signup scenarios) is a single configuration switch, off by default.

**Ownership.** The owner of an order is the token's `sub`, recorded at creation from the validated token and never taken from the request body. Ownership never changes. Responses expose it as `ownerId`.

**Order of checks and status codes.**
1. The caller's roles lack the permission for the action at all → `403 Forbidden`, before any data is read.
2. The order doesn't exist, or the caller only has `order:read:own` and isn't the owner → `404 Not Found`. Orders a caller may not see are indistinguishable from orders that don't exist, so their existence never leaks (OWASP API1: Broken Object Level Authorization). IDs are random UUIDs as well, but the `404` doesn't rely on that.
3. The requested status change isn't allowed from the order's current status → `409 Conflict`.

Listing (`GET /api/v1/orders`) is filtered to the caller's own orders unless they have `order:read:any`. It never errors because other orders exist.

## Considered options

- **Role source.**
  - *Custom user attribute* (`custom:role`): rejected. Cognito puts custom attributes in ID tokens, not access tokens, unless a pre-token-generation Lambda customises the access token, which needs a paid Cognito feature plan on AWS.
  - *OAuth scopes on a resource server*: rejected as the role mechanism. Scopes describe what a **client application** may do, not what a **person** is, and the password sign-in flow only issues `aws.cognito.signin.user.admin`. Scopes stay a natural complement: in a production setup, client scopes would further narrow what a token may do.
  - *Role table in our own database*: rejected for now. It gives instant revocation at the cost of a read per request and a provisioning flow. It is the path forward if instant revocation becomes a requirement.
- **Several roles.** *Highest role wins* gives the same answer while roles are nested, but breaks as soon as a non-nested role (e.g. a read-only AUDITOR) is added. Union doesn't.
- **No recognised role.** *Default to USER* suits consumer self-signup but lets anyone who obtains a token from the pool (e.g. through a misconfigured app client) place orders. Kept as an opt-in switch.
- **Foreign orders.** *`403`* confirms the order exists. *Configurable* adds complexity without a use case.
- **Owner identity.**
  - *`username` / `email`*: mutable and provider-specific. A user who changes their email would lose their orders.
  - *Internal user ID mapped from (issuer, `sub`)*: survives a provider switch, but needs a users table and first-login provisioning. Speculative for this scope.

## Consequences

- **Switching identity provider orphans existing orders.** `sub` is unique only per issuer, and a new provider issues new `sub` values. A migration to another provider must map old subjects to new ones (a one-off subject migration), or the design must first move to an internal user ID mapped from (issuer, `sub`). This is the price of storing `sub` directly and is accepted knowingly.
- **Role changes lag by up to one access-token lifetime.** Roles are read from the token, so a demoted user keeps the old role until the token expires. Mitigations: a short access-token lifetime (e.g. 15 minutes) and `AdminUserGlobalSignOut`, which revokes refresh tokens. Instant revocation needs the database-backed role table above.
- **What must be configurable for provider portability:**
  - **Where the roles claim lives:** `cognito:groups` (Cognito), `realm_access.roles` (Keycloak), `groups` (Okta), `roles` (Entra ID).
  - **The group → role mapping.**
  - **How the calling client is identified:** Cognito access tokens carry `client_id` and no `aud`; Keycloak and Entra ID use `aud` / `azp`.
- **API Gateway's built-in Cognito authorizer works against the portability goal.** It only understands Cognito. Token validation itself is decided separately, but standard OIDC validation (issuer discovery + JWKS) keeps the provider swappable.
