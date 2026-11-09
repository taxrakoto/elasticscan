# Dependency Injection

## Status and problem

US-001.1 provides a reusable authentication dependency; US-002.1 composes it with
team authorization. Job operations need the same verified identity and
membership rules. Repeating these rules inside each handler would make them
harder to maintain consistently.

Dependency Injection means a component receives its collaborators instead of
constructing or locating them itself. Here, FastAPI calls the declared dependency
functions and supplies their results to `require_team_context`.
US-005.1 reuses the same pattern for initial-target validation.
US-003.2 composes these dependencies in production submission and injects the
database session factory.

## Components and flow

- `app/core/auth.py`: `require_identity` authenticates the bearer token using
  `get_token_validator` and returns verified claims.
- `app/core/team.py`: `get_team_settings` supplies operator-configured claim
  mapping. `require_team_context` receives settings, verified identity, and the
  `X-Team-ID` header, then calls `resolve_team_context`.
- The resolver validates membership and selection, returning a `TeamContext` or
  raising a uniform HTTP 403. Failed authentication returns HTTP 401 first.
- `app/api/routes/scans.py` declares `Depends(require_team_context)` and uses its
  result for immutable job ownership. FastAPI caches this dependency within the
  request, sharing the result with the target-policy dependency.
- `app/core/target_policy.py`: `require_allowed_submission` receives authorized
  team context, a typed submission, and the configured `TargetPolicy`. It applies
  exact-origin and public-address checks before returning the submission to a
  production submission route. Tests inject a controlled resolver into the policy and override
  the policy dependency without bypassing its actual validation rules.
- `app/api/dependencies.py` supplies the application-owned session factory from
  request state. Startup creates its engine/pool; shutdown disposes the pool.
  The acceptance service opens and closes a session and commits its transaction.
  The route receives collaborators rather than constructing a pool per request.
- Tests override the factory to use a migrated disposable PostgreSQL schema,
  retaining real authorization, target checks, and acceptance persistence.

The HTTP boundary is a policy enforcement point: a denied request does not reach
the protected handler. Fail-closed authorization is a security principle, rather
than another design pattern.

## Benefits, tradeoffs, and limits

The dependencies centralize authorization and expose it in OpenAPI. Tests can
replace settings or the validator through FastAPI's dependency overrides while
retaining the real team authorization rules.

The declaration order and dependency graph matter: membership is trusted only
after authentication. The pure resolver assumes its input claims were verified;
calling it directly on client-provided JSON would violate that boundary.

A route must explicitly declare the dependency. This pattern does not
automatically protect future endpoints, filter database queries, or enforce
worker ownership. Retrieval explicitly filters through its repository query;
worker integration remains later work. Direct callers of
the acceptance service must still supply trusted inputs; injection does not
enforce that trust outside the HTTP boundary. Application state requires startup
initialization or an explicit test factory override.
Likewise, initial-target checks do not constrain later DNS changes, redirects,
or crawler traffic. The production submission route declares the policy
dependency; future submission routes must do so too. Scanner/egress enforcement
remains US-005.2. Default deny and defense in depth
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
`tests/integration/test_scan_acceptance.py` additionally verifies production
HTTP 202 after commit, persisted ownership, denied and malformed input without
records, safe HTTP 503 with rollback, and replacement application/pool persistence.
These support the submission portions of AC-001/AC-002, not GET retrieval or a
full process-restart demonstration. The existing Transactional Outbox, Repository,
and Value Object patterns are composed without interface changes. FastAPI is a
framework, atomic transactions are a database technique, and default deny is a
security principle rather than a new design pattern.
See [Authentication and team context](../authentication.md) and the related
[Value Object](value-object.md).


## Production retrieval composition (US-009.1)

`retrieve_scan` receives verified context and the existing session factory, owns a
read session, and calls the team-filtered repository query. It does not depend on
submission target policy: reading a saved job requires team membership, not
new DNS resolution or current target allowlist approval. `TeamContext` remains
a reused immutable Value Object; its creator field does not restrict peer reads.

`tests/integration/test_job_retrieval.py` verifies the production GET dependency
composition: 401/403 and invalid UUID 422 before repository lookup, authorized
team selection, peer access, unknown/cross-team 404 equivalence, and safe 503.
Tests replace storage configuration and external key/target responses while
retaining actual JWT verification and team-context resolution. Query filtering
is performed by the Repository pattern, not automatically by dependency injection.
Future endpoints must explicitly declare these dependencies and use restricted
queries. Full lifecycle/worker isolation remains subsequent work.
