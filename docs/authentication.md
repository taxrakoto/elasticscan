# Authentication and authorized team context (US-001.1, US-002.1)

`app/core/auth.py` provides `TokenValidator` and the FastAPI `require_identity`
dependency. They authenticate JWT access tokens using the existing PyJWT/crypto
dependency. `app/core/team.py` adds `require_team_context` to resolve authorized
team membership after authentication. Attaching protection to each job endpoint
and the final endpoint regression remain US-001.3; ownership persistence and full
job-isolation acceptance remain US-002.2/US-002.3.

## Trust configuration

The API validates configuration at startup and fetches keys only when a protected
request needs them. Missing/invalid trust settings prevent startup. Importing the
application does not contact the provider.

| Environment variable | Meaning | Local Compose value |
| --- | --- | --- |
| `OIDC_ISSUER` | Exact expected `iss` claim | `http://localhost:8080/realms/elasticscan` |
| `OIDC_AUDIENCE` | Required API recipient in `aud` | `elasticscan-api` |
| `OIDC_JWKS_URL` | Operator-configured signing-key endpoint | `http://keycloak:8080/realms/elasticscan/protocol/openid-connect/certs` |
| `OIDC_ALGORITHMS` | Comma-separated signing-algorithm allowlist | `RS256` |
| `OIDC_TEAM_CLAIM` | Exact top-level verified claim holding a team-ID array; defaults to `team_ids` when unset | `team_ids` |

The implementation supports the RSA signing algorithms RS256, RS384, and RS512;
only explicitly configured algorithms are accepted. Local development allows
RS256 alone. Symmetric HS algorithms and unsigned tokens are rejected. Keep the
trusted provider's API audience restricted to API access tokens; its ID tokens
are not a substitute. The local realm maps this audience into access tokens only.

The container-internal JWKS hostname and public issuer intentionally differ.
An unverified token never determines either URL. Its `kid` selects a key only
from the configured JWKS endpoint. Token `jku`, `x5u`, and embedded `jwk` headers
cannot replace that source. Deployment configuration is trusted operator input;
use HTTPS outside the isolated local development stack.

## Validation and HTTP behavior

The validator verifies the signature, allowlisted algorithm, matching key
algorithm, exact issuer, API audience, expiration, and `nbf` when present. `iat`
is validated when present. Required claims are `iss`, `aud`, `exp`, and a nonempty
string `sub`. Time claims must be finite JSON numbers. Clock leeway is zero.

Missing/malformed bearer credentials and token validation failures return:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{"detail":"Invalid or missing access token"}
```

The response includes neither the token nor provider/key/claim failure details.
Key-fetch failure also denies authentication. No unverified claims reach the
handler. The synchronous dependency lets FastAPI run blocking JWKS retrieval in
its thread pool.

Attach the team dependency when adding a job route:

```python
from typing import Annotated
from fastapi import Depends
from app.core.team import TeamContext, require_team_context

def job_handler(
    context: Annotated[TeamContext, Depends(require_team_context)],
):
    team_id = context.team_id
    subject = context.created_by
    # Future persistence operations must use the authorized team_id.
```

The dependency declares HTTP bearer security in that route's OpenAPI definition.
The production application currently has only a public `/health/live` route;
tests use separate `/protected` and `/team-protected` routes. No diagnostic
identity endpoint or scan endpoint is introduced by these stories.

## Team membership mapping and selection

The operator sets `OIDC_TEAM_CLAIM` to an exact top-level claim name. The default
is `team_ids`. An explicitly empty or whitespace-padded configuration is invalid
and prevents startup. A name containing a dot is treated literally, not as a
nested path. There is no fallback to another claim when the configured claim is
missing. Team settings are lightweight and read from the process environment by
the dependency; deploy configuration changes by restarting the API.

The trusted provider must issue membership as a nonempty JSON array of nonempty
strings with no surrounding whitespace. For example:

```json
{"sub": "verified-user-id", "team_ids": ["team-alpha", "team-beta"]}
```

Team IDs are opaque, case-sensitive strings. Duplicate membership entries count
as one team. Scalars, objects, empty arrays, and arrays containing any malformed
entry are denied; valid entries are not salvaged from a malformed array. Strings
are not coerced, trimmed, or split on commas.

| Verified membership | `X-Team-ID` header | Result |
| --- | --- | --- |
| One distinct team | Absent | Resolve that team |
| One or multiple teams | Exactly one authorized team | Resolve the selected team |
| Multiple distinct teams | Absent | HTTP 403 |
| Missing, empty, or malformed membership | Any | HTTP 403 |
| Valid membership | Unauthorized, blank, or repeated selector | HTTP 403 |
| Missing or invalid bearer token | Any | HTTP 401 with Bearer challenge |

`X-Team-ID` selects among verified memberships; it grants no membership. Repeated
headers are rejected, even if their values match. A comma-containing value is a
single literal ID, not a list of choices, and succeeds only if that exact ID is
present in the verified membership array. The header is optional in OpenAPI
because single-team callers can omit it.

All context denials use the same safe response:

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"detail":"Missing or unauthorized team context"}
```

The response reveals neither membership lists nor claim failure details.
Successful resolution returns an immutable `TeamContext(team_id, created_by)`.
`created_by` is the current caller's verified `sub`, suitable for assigning a
new job's creator; it must not overwrite an existing job's creator on reads or
other operations.

### Trust and scope

Local Keycloak maps its `team_ids` user attribute to access-token membership.
Provisioning and profile permissions must prevent users from editing trusted
membership attributes themselves. Signature verification authenticates the
provider's claims; it cannot repair an unsafe membership provisioning policy.
Production providers must establish the same administrator-controlled mapping.

Existing JWTs retain their signed membership until expiration. This resolver
does not perform live membership lookups or immediate revocation. It also does
not implement roles, administrative cross-team access, job persistence, or
team-scoped queries. A selected authorized team still must be used as a query
constraint; later cross-team job access must return the same HTTP 404 as an
unknown job. Denying an unauthorized selector with HTTP 403 does not reveal a
job's existence.

The implementation uses [Dependency Injection](design-pattern/dependency-injection.md)
and an immutable [Value Object](design-pattern/value-object.md). Denying unresolved
context is the fail-closed security principle.

## Key-cache policy and scope

One validator is shared per API process. PyJWT caches the JWKS set for 300 seconds,
uses a five-second fetch timeout, and refreshes from the same configured endpoint
when a key ID is unknown. Permanent per-key caching is disabled. Keys already in
a valid cache can be used during a brief provider outage; an unavailable required
key fails closed. Configuration changes require an API restart.

These are explicit implementation defaults, not completed production rotation
acceptance. US-001.2 covers the trusted key-rotation exercise and operational
policy. Team membership claims are interpreted only after token verification;
US-002.1 uses `OIDC_TEAM_CLAIM` to authorize the selected team.

## Verification

Offline tests generate ephemeral RSA keys, sign actual JWTs, and exercise the real
validator and a test-only FastAPI route. They check valid input and invalid
signature, algorithm, issuer, audience, lifetime, missing claims, key identifiers,
malformed tokens/JWKS, and token-supplied key-source headers. Team tests cover
single/multiple memberships, explicit selection, malformed/missing membership,
duplicate headers, custom mapping without fallback, immutable context, OpenAPI,
startup validation, and rejection before handler execution.

```sh
docker run --rm --network none --read-only -v "$PWD:/workspace:ro" -w /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_token_validation tests.integration.test_authentication tests.unit.test_team_context tests.integration.test_team_authorization -v
```

For live local Keycloak verification alongside all existing tests:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_KEYCLOAK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

The live tests obtain access tokens for `alice`, `alice-peer`, and `bob`, fetch the
real configured JWKS, authenticate through the dependency, and reject wrong
issuer/audience, a corrupted signature, and missing credentials. The expiry test
advances only the verifier's clock, preserving the real provider signature.
No credentials or tokens are printed. Tests refuse nondefault provider URLs
before submitting fixture passwords. Team checks resolve Alice and Alice's peer
to `team-alpha`, Bob to `team-beta`, and reject cross-team selectors. These tests
establish team-context authorization, not complete team-scoped job isolation.

If local Keycloak reports healthy but token issuance fails with an internal
missing-table error, recreate its development-only container:

```sh
docker compose up -d --no-deps --force-recreate --wait --wait-timeout 120 keycloak
```

Its in-memory database is rebuilt from the configured realm import. Development
sessions and signing keys change; restart API processes to clear their key caches.
This does not reset the separate PostgreSQL database or its volume.

References: [PyJWT API](https://pyjwt.readthedocs.io/en/2.10.1/api.html),
[PyJWT usage](https://pyjwt.readthedocs.io/en/2.10.1/usage.html), and
[FastAPI security tools](https://fastapi.tiangolo.com/reference/security/).
