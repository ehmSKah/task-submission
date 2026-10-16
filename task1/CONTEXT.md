# Orders API

An API through which users place and track their own orders, and staff manage orders on everyone's behalf. Who may do what is decided by roles asserted by an external identity provider.

## Access

**Identity Provider**:
The external OpenID Connect service that authenticates people and issues their access tokens. Amazon Cognito today; meant to be swappable (e.g. Keycloak, Okta, Microsoft Entra ID).
_Avoid_: Auth server, user pool (a Cognito-specific detail)

**Role**:
What a person is in this API: `USER`, `ORDER_MANAGER` or `ADMIN`. Owned by this API's vocabulary, not by the identity provider. A person may hold several.
_Avoid_: Group (the identity provider's mechanism for carrying a role), user type

**Group**:
The identity provider's way of tagging a person, from which their roles are derived. Only meaningful at the boundary with the identity provider.
_Avoid_: Using "group" to mean a role inside the API

**Permission**:
A single allowed action on orders, granted to a role. Access checks ask about permissions, never about roles directly.
_Avoid_: Right, privilege, scope (scopes describe what a client application may do, not what a person may do)

## Orders

**Owner**:
The person who created an order, identified by their subject (`sub`) at the identity provider. Fixed for the order's lifetime.
_Avoid_: Customer, creator, user (ambiguous with the `USER` role)

**Cancellation**:
A business status change that ends an order's lifecycle while keeping its record. Any owner may cancel their own order.
_Avoid_: Delete, remove

**Deletion**:
Removal of an order's record. An administrative act, not part of the order's lifecycle.
_Avoid_: Cancel, archive
