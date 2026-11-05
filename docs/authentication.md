# Trusted access-token validation (US-001.1)

`app/core/auth.py` provides `TokenValidator` and the FastAPI `require_identity`
dependency. They authenticate JWT access tokens using the existing PyJWT/crypto
dependency. Team authorization remains US-002.1; attaching protection to each job
endpoint and the final endpoint regression remain US-001.3.

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

Attach the dependency when adding a job route:

```python
from typing import Annotated
from fastapi import Depends
from app.core.auth import require_identity

def job_handler(identity: Annotated[dict, Depends(require_identity)]):
    subject = identity['sub']
    # Resolve authorized team context before accessing jobs (US-002.1).
```

The dependency declares HTTP bearer security in that route's OpenAPI definition.
The production application currently has only a public `/health/live` route;
tests use a separate `/protected` route. No diagnostic identity endpoint or scan
endpoint is introduced by this story.

## Key-cache policy and scope

One validator is shared per API process. PyJWT caches the JWKS set for 300 seconds,
uses a five-second fetch timeout, and refreshes from the same configured endpoint
when a key ID is unknown. Permanent per-key caching is disabled. Keys already in
a valid cache can be used during a brief provider outage; an unavailable required
key fails closed. Configuration changes require an API restart.

These are explicit implementation defaults, not completed production rotation
acceptance. US-001.2 covers the trusted key-rotation exercise and operational
policy. Team membership claims are returned only after token verification;
US-002.1 will interpret `OIDC_TEAM_CLAIM` and authorize the selected team.

## Verification

Offline tests generate ephemeral RSA keys, sign actual JWTs, and exercise the real
validator and a test-only FastAPI route. They check valid input and invalid
signature, algorithm, issuer, audience, lifetime, missing claims, key identifiers,
malformed tokens/JWKS, and token-supplied key-source headers.

```sh
docker run --rm --network none --read-only -v "$PWD:/workspace:ro" -w /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_token_validation tests.integration.test_authentication -v
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
before submitting fixture passwords. They establish authentication and signed
membership claims, not team-scoped job authorization.

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
