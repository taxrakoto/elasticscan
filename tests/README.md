# US-003.1: submission-schema TDD

These tests describe the first submission-schema behavior for US-003.1, tracing
to FR-001/FR-002 and the relevant validation behavior in AC-002. The schema implementation
is in `app/schemas/submission.py`, with its public types exported by `app.schemas`.

The public interface is `app.schemas.ZapSubmission`, a Pydantic model
with `model_validate`, `model_dump`, and `model_dump_json`. Tests exercise public
input/output behavior rather than private validators or model configuration.

`fixtures/contracts/v1/scan-submission.json` versions the test request examples.
It covers HTTP/HTTPS ZAP requests; missing fields; unsupported tools; malformed,
empty, non-string, or non-HTTP URLs; invalid parameter objects; and unknown root
and nested fields, including an untrusted ownership field.

Unit tests check typed validation, field-level error details, and JSON output.
Integration tests embed a small FastAPI application with a test-only `/scan`
route. They verify that FastAPI validates `ZapSubmission`, returns structured
HTTP 422 for invalid input, and does not call the handler when validation fails.
Valid input reaches the handler as a typed model. The handler spy records calls
only; it does not persist jobs or perform scanning.

These tests cover the integration of the Pydantic schema with FastAPI. They do
not exercise `app.main.app`, which exposes only the bootstrap liveness endpoint.

## Run

Use the pinned application image built for local development. No running services,
AWS credentials, database, network, pytest, or additional packages are required:

```sh
docker build -f docker/api/Dockerfile -t elasticscan-api:local .
docker run --rm --network none --read-only --mount "type=bind,src=$PWD,dst=/workspace,readonly" --workdir /workspace --entrypoint python elasticscan-api:local -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

The bind mount runs the current source and tests. The runner uses Python's standard
`unittest` library and the application's existing locked FastAPI/Pydantic packages.

## TDD verification

The initial red run produced five assertion failures stating that `app.schemas`
must expose `ZapSubmission`; discovery and imports succeeded. No test was skipped
or marked as an expected failure.

On 2026-11-05, the schema and embedded-FastAPI integration suite passed all
five tests using two valid and 21 invalid request examples. The embedded test
application is the retained integration-test scope; the production `/scan`
endpoint has been removed.

`ZapSubmission` requires the literal scan type `zap` and nested `ZapParameters`.
Both models forbid extra fields; `AnyHttpUrl` validates HTTP/HTTPS URL syntax and
provides JSON serialization. The models perform no DNS lookups or persistence.

The fixture contract version identifies the test examples; it does not add a
version field to the public submission JSON or establish the other cross-component
contracts mentioned in the story's sequencing note.

## US-004.1: PostgreSQL storage

`integration/test_job_storage.py` adds six real PostgreSQL tests. They cover
migration/model agreement, repeated upgrade and downgrade/re-upgrade, committed
job/intent reload through a fresh connection pool, database immutability and
state constraints, execution/outcome storage, and dispatch uniqueness/foreign keys.
Each test migrates and removes its own disposable schema.

Run the complete suite with the opt-in database flag:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

PostgreSQL must be healthy and the API image rebuilt with the updated lock.
Without the flag, the six database tests are explicitly skipped. On 2026-11-05,
the full suite passed all 11 tests against PostgreSQL 16.6. See
[Database migrations](../docs/database-migrations.md) for schema and deployment details.

## US-001.1: trusted token validation

- `unit/test_token_validation.py` verifies signatures and claims using ephemeral
  RSA keys and a controlled JWKS response, with no network.
- `integration/test_authentication.py` verifies a test-only protected FastAPI route,
  uniform HTTP 401 with a Bearer challenge, OpenAPI security, startup configuration
  validation, and public production liveness without key retrieval.
- `integration/test_keycloak_authentication.py` uses actual local provider tokens
  and JWKS. Enable it with `RUN_KEYCLOAK_TESTS=1`; otherwise it is explicitly skipped.

Run all tests, including live PostgreSQL and Keycloak:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_KEYCLOAK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

These checks do not create production scan routes or establish team authorization.
See [Authentication](../docs/authentication.md) for configuration and coverage.
