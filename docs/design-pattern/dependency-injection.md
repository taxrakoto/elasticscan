# Dependency Injection

## Status and problem

US-001.1 provides a reusable authentication dependency; US-002.1 composes it with
team authorization. Future job operations need the same verified identity and
membership rules. Repeating these rules inside each handler would make them
harder to maintain consistently.

Dependency Injection means a component receives its collaborators instead of
constructing or locating them itself. Here, FastAPI calls the declared dependency
functions and supplies their results to `require_team_context`.
US-005.1 reuses the same pattern for initial-target validation.

## Components and flow

- `app/core/auth.py`: `require_identity` authenticates the bearer token using
  `get_token_validator` and returns verified claims.
- `app/core/team.py`: `get_team_settings` supplies operator-configured claim
  mapping. `require_team_context` receives settings, verified identity, and the
  `X-Team-ID` header, then calls `resolve_team_context`.
- The resolver validates membership and selection, returning a `TeamContext` or
  raising a uniform HTTP 403. Failed authentication returns HTTP 401 first.
- Future job handlers declare `Depends(require_team_context)` and use its team
  for persistence access. The production application currently has no job routes.
- `app/core/target_policy.py`: `require_allowed_submission` receives authorized
  team context, a typed submission, and the configured `TargetPolicy`. It applies
  exact-origin and public-address checks before returning the submission to a
  future route. Tests inject a controlled resolver into the policy and override
  the policy dependency without bypassing its actual validation rules.

The HTTP boundary is a policy enforcement point: a denied request does not reach
the protected handler. Fail-closed authorization is a security principle, rather
than another design pattern.

## Benefits, tradeoffs, and limits

The dependency centralizes authorization and exposes it in OpenAPI. Tests can
replace settings or the validator through FastAPI's dependency overrides while
retaining the real team authorization rules.

The declaration order and dependency graph matter: membership is trusted only
after authentication. The pure resolver assumes its input claims were verified;
calling it directly on client-provided JSON would violate that boundary.

A route must explicitly declare the dependency. This pattern does not
automatically protect future endpoints, filter database queries, or enforce
worker ownership. Those integrations remain later stories.
Likewise, initial-target checks do not constrain later DNS changes, redirects,
or crawler traffic. Future submission routes must declare the policy dependency;
scanner/egress enforcement remains US-005.2. Default deny and defense in depth
describe security principles used by this policy, not new design patterns.

## Verification and requirements

`tests/unit/test_team_context.py` exercises mapping and selection rules.
`tests/integration/test_team_authorization.py` exercises signed JWTs, header
selection, dependency overrides, OpenAPI, startup configuration, and denied
requests not reaching handlers. The opt-in
`tests/integration/test_keycloak_authentication.py` verifies provider-issued
memberships for Alice, Alice's peer, and Bob.

`tests/unit/test_target_policy.py` and
`tests/integration/test_target_policy.py` verify controlled IPv4/IPv6 resolution,
safe policy denial, typed URL errors, and no DNS for unauthorized callers. See
[Initial target policy](../target-policy.md) for configuration and scope under
NFR-006–008 and the initial-validation portion of AC-010/AC-018.

This supports FR-027 and the context-resolution parts of FR-006, FR-028, FR-033,
and AC-016/AC-017. Complete job isolation is not established by these tests.
See [Authentication and team context](../authentication.md) and the related
[Value Object](value-object.md).
