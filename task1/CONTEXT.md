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

**Caller**:
The person making a request, as the identity provider has established them: their subject and their recognised roles.
_Avoid_: Principal (an AWS policy term), current user

**Permission**:
A single allowed action on orders, granted to a role. Access checks ask about permissions, never about roles directly.
_Avoid_: Right, privilege, scope (scopes describe what a client application may do, not what a person may do)

**Rate Limit**:
How many requests a client may make per minute, counted per client IP and per caller. Over it, the API answers `429` until the minute is over.
_Avoid_: Throttling (AWS's own term for capacity limits), quota (a longer-term allowance)

**Tier**:
A role's per-caller rate limit. A caller with several roles gets the highest of their tiers; one without a role gets the lowest.
_Avoid_: Plan, level

## Orders

**Order**:
A request by its owner for a set of line items in one currency, tracked through a status lifecycle.
_Avoid_: Purchase, transaction, basket

**Line Item**:
One product reference (SKU) on an order, with a quantity and the unit price the owner was quoted.
_Avoid_: Position, entry, product

**Order Total**:
The sum of quantity × unit price over all line items, always computed by the API, never supplied by a client.
_Avoid_: Amount, price

**Order Status**:
Where an order is in its lifecycle: `PENDING`, `CONFIRMED`, `SHIPPED`, `DELIVERED` or `CANCELLED`. Moves forward only; `DELIVERED` and `CANCELLED` are final.
_Avoid_: State, stage

**Status Change**:
Moving an order from one status to an allowed next status. Requesting the status an order already has changes nothing.
_Avoid_: Transition (fine in code, not in conversation), update

**Owner**:
The person who created an order, identified by their subject (`sub`) at the identity provider. Fixed for the order's lifetime.
_Avoid_: Customer, creator, user (ambiguous with the `USER` role)

**Cancellation**:
The status change to `CANCELLED`, possible until an order ships. Ends the order's lifecycle while keeping its record. Any owner may cancel their own order.
_Avoid_: Delete, remove

**Deletion**:
Removal of an order's record. An administrative act, not part of the order's lifecycle.
_Avoid_: Cancel, archive
