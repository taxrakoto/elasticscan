# Submission contract (US-003.3)

## Current scope and discovery

The production `POST /scan` endpoint authenticates the caller, selects an authorized
team, validates the initial target, and durably saves a job and dispatch intent.
Dispatch and scanner execution remain pending implementation. Acceptance does not
mean a scan has run or completed.

OpenAPI is available at `/openapi.json`; interactive documentation is available at
`/docs` (Swagger UI) and `/redoc`. The application generates this contract from its
route declarations and typed models. Endpoint documentation constants live in
`app/api/docs/submission.py`; examples and field descriptions live in
`app/schemas/submission.py`.

## Request

Send `Content-Type: application/json` with:

```json
{
  "scan_type": "zap",
  "parameters": {
    "url": "https://approved.example.invalid/"
  }
}
```

Both root fields and `parameters.url` are required. Only `zap` is supported. The
URL must be a non-empty HTTP or HTTPS URL. Unknown fields at either object level,
including ownership fields such as `team_id` and `created_by`, return HTTP 422.
Ownership comes from verified identity and team context, never request JSON.

This exact schema example is an isolated test fixture. The `.invalid` hostname
is not a live target and will not succeed against a normal deployment. Tests
explicitly allow its origin and supply controlled public DNS results; no scanner
runs. For real use, substitute a target you are authorized to scan whose origin
is configured in the deployment allowlist and whose addresses pass policy.

## Bearer authentication

Send `Authorization: Bearer <access-token>` with an API access token from the
configured trusted issuer. The application verifies its signature, issuer,
audience, lifetime, and required claims. Missing or invalid authentication returns:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{"detail":"Invalid or missing access token"}
```

See [Authentication and team context](authentication.md) for trust configuration.

## Team selection

The operator configures the verified membership claim through `OIDC_TEAM_CLAIM`,
which defaults to `team_ids`. A valid claim is a nonempty array of nonempty,
case-sensitive team IDs without surrounding whitespace.

- One distinct membership: omit `X-Team-ID` or select that team explicitly.
- Multiple distinct memberships: send exactly one `X-Team-ID` selecting a membership.
- Missing, malformed, or ambiguous membership/context, unauthorized selection,
  and repeated selectors are denied with HTTP 403.

`X-Team-ID` selects membership; it grants none. Duplicate membership entries count
as one team. Repeated headers are rejected even when their values match. The
header is optional in OpenAPI because single-team callers can omit it.

The example verification signs a token with `team-alpha` and `team-beta`, sends
`X-Team-ID: team-alpha`, and verifies that ownership is saved for `team-alpha`
and the token's verified subject.

## Target authorization

After authentication, team resolution, and schema validation, the initial URL
must pass the configured exact-origin allowlist and public-address checks.
Syntactically valid but disallowed targets return HTTP 403. See
[Initial target policy](target-policy.md) for normalization and address rules.
Initial validation does not establish redirect, DNS-change, or crawl-time controls;
scanner/egress enforcement remains later work.

## Durable acceptance

Successful submission returns HTTP 202 with a response shaped like:

```json
{
  "job_id": "123e4567-e89b-42d3-a456-426614174000",
  "scan_type": "zap",
  "status": "pending"
}
```

The UUID is illustrative; each accepted request receives a generated job ID.
The job and dispatch intent commit before the response is returned. `pending`
is the acceptance snapshot, independent of later worker progress. Use the
team-protected `GET /scan/{job_id}` endpoint for the current persisted status.

## Errors

| HTTP status | Meaning | JSON body |
| --- | --- | --- |
| 401 | Missing or invalid authentication | `{"detail":"Invalid or missing access token"}` |
| 403 | Team context denied | `{"detail":"Missing or unauthorized team context"}` |
| 403 | Initial target denied | `{"detail":"Target is not allowed"}` |
| 422 | Request schema validation failed | Structured `detail` array |
| 503 | Database acceptance could not be confirmed | `{"detail":"Durable acceptance could not be confirmed"}` |

For example, `{"scan_type":"zap","parameters":{}}` produces a missing-URL error:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "parameters", "url"],
      "msg": "Field required",
      "input": {}
    }
  ]
}
```

`loc` identifies the invalid field, `msg` explains the issue, and `type` identifies
the category. Additional fields vary by validation category. Missing fields,
unsupported scan types, malformed URLs, and extra fields produce structured 422
errors. Authentication or authorization can reject a request before body validation.

A 503 does not prove the job was never saved: connection loss during commit can
leave acceptance uncertain. Automatic retry deduplication is not implemented;
resubmitting can create another job.

## Verification and acceptance limits

With the local PostgreSQL service healthy, run from `elasticscan/`:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_scan_acceptance -v
```

The OpenAPI tests check request/response schemas, bearer security, the team header,
and documented errors. The PostgreSQL example test reads the request directly
from generated OpenAPI and submits it through the production route. It retains
real signature verification, team resolution, target checks, and atomic database
persistence, while replacing external key/DNS responses and storage configuration.
The response example is also validated as a `ScanAcceptance`.

Each database test migrates and removes its own unique schema. No permanent
demonstration records or actual scans are created. Without `RUN_POSTGRES_TESTS=1`,
database tests skip explicitly; OpenAPI tests still run. Mounted source checks do
not rebuild or replace the running API image. Rebuild that image separately to
serve the updated documentation in the running stack.

Existing tests cover malformed requests and denials without unintended jobs,
commit-before-response ordering, rollback, and application/pool replacement.
US-003.3 verifies the submission contract portions of FR-001/002/004/007 and
AC-001/002; it does not establish full process-restart, end-to-end dispatch,
scanner execution, or complete lifecycle acceptance. See the
[test guide](../tests/README.md) and
[Dependency Injection](design-pattern/dependency-injection.md) for the reused
pattern and verification boundaries.
