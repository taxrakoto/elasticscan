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

US-002.2 extends the invalid examples from 21 to 24, adding creator and nested
ownership fields alongside the existing root-level team field. These examples
verify that neither `team_id` nor `created_by` can enter at either request level.

Unit tests check typed validation, field-level error details, and JSON output.
Integration tests embed a small FastAPI application with a test-only `/scan`
route. They verify that FastAPI validates `ZapSubmission`, returns structured
HTTP 422 for invalid input, and does not call the handler when validation fails.
Valid input reaches the handler as a typed model. The handler spy records calls
only; it does not persist jobs or perform scanning.

These tests cover the integration of the Pydantic schema with FastAPI. They do
not exercise `app.main.app`. US-003.2 adds separate production-route checks below.

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

These checks do not create production scan routes or establish full job isolation.
See [Authentication](../docs/authentication.md) for configuration and coverage.

## US-002.1: authorized team context

- `unit/test_team_context.py` exercises single-team inference, duplicate
  membership, explicit multi-team selection, malformed/missing membership,
  unauthorized selectors, custom mapping without fallback, environment
  configuration, and immutable context.
- `integration/test_team_authorization.py` uses signed JWTs and test-only
  `/team-protected` handlers. It checks HTTP 401/403, no handler execution on
  denial, custom claim mapping, OpenAPI security/header metadata, and startup
  rejection of invalid team configuration.
- `integration/test_keycloak_authentication.py` extends opt-in live verification
  to Alice and Alice's peer in `team-alpha` and Bob in `team-beta`, with implicit
  selection, matching explicit selection, and rejected cross-team selectors.

The complete offline command above discovers these tests without external
services; PostgreSQL and Keycloak checks are skipped unless their flags are set.
For focused authentication/team checks:

```sh
docker run --rm --network none --read-only -v "$PWD:/workspace:ro" -w /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_token_validation tests.integration.test_authentication tests.unit.test_team_context tests.integration.test_team_authorization -v
```

The live command above enables both service suites. These checks establish the
team-context foundation of FR-027 and AC-016/AC-017, not submission, persisted job
ownership, or full isolation of retrieve/list/cancel/delete operations. No
production team diagnostic endpoint is created.

On 2026-11-06, the focused authentication/team suite passed all 23 tests. Full
offline discovery passed 28 tests with 10 service-dependent tests skipped. With
both integration flags enabled, all 38 tests passed, including real PostgreSQL
and Keycloak. Before the successful live run, Keycloak's documented missing-table
fault returned HTTP 500 during token issuance; recreating only its development
container from the existing realm import restored the provider. No application
code change was needed for that fault.

## US-002.2: persisted immutable ownership

`integration/test_job_storage.py` adds three PostgreSQL tests:

- A signed JWT is verified and resolved into `TeamContext`, then passed with a
  validated submission to `create_pending_job`. Committed ownership is reloaded
  through a fresh connection pool for two teams, two members of one team, and a
  multi-team caller with explicit selection.
- Both `team_id` and `created_by` changes are rejected through ORM object edits,
  bulk updates, and direct SQL. Each attempt must fail with SQLSTATE `23514`, and
  original ownership is reloaded after each rollback.
- Permitted execution metadata updates through ORM and raw SQL preserve ownership.

Run the PostgreSQL suite using the live command above, or focus on storage with:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_job_storage -v
```

The existing submission unit and HTTP tests automatically consume the new
ownership rejection examples; HTTP validation must return 422 without calling
the handler. The test-only submission route does not persist jobs. PostgreSQL
tests use isolated schemas and the real initial migration; no migration change
is needed for this task.

These checks establish verified ownership on creation and database transfer
protection, not a production submission endpoint, atomic outbox acceptance,
team-scoped read API, or deployed worker/maintenance roles. Restricted database
service identities and actual worker processing are subsequent work.

On 2026-11-06, all five submission schema/HTTP tests passed with two valid and
24 invalid examples, and all nine PostgreSQL storage tests passed. Full discovery
with PostgreSQL and Keycloak enabled passed all 41 tests. The first full run
passed all 37 non-Keycloak tests but failed during Keycloak setup with HTTP 500;
its log confirmed the known missing session-table fault. Recreating the local
development Keycloak container from the existing realm import restored token
issuance and the subsequent full run passed. Syntax, fixture validity/unique
names, local documentation links, and Git whitespace checks also passed.

## US-004.2: atomic database acceptance

`app/services/acceptance.py` coordinates the job and dispatch intent in one
transaction, returning `AcceptedScan` only after commit. Five additional real
PostgreSQL tests in `integration/test_job_storage.py` verify:

- A committed pending job and its unpublished intent reload through a fresh pool.
- A simulated failure after the job insert leaves neither record committed.
- A real constraint violation on dispatch insertion rolls back the job too.
- A simulated failure after both inserts just before commit rolls back both.
- Another connection sees neither record before commit, then sees both afterward.

Each test migrates its own disposable schema. Patches and transaction event
listeners inject failures only in tests and are removed afterward. These checks
verify the reusable database service; they do not create a public scan endpoint,
permanent demonstration jobs, or a working publisher.

Run the storage-only command above for focused checks, or enable PostgreSQL and
Keycloak with the full-suite command above. HTTP acknowledgment, API restart,
SNS outage/retry, and worker recovery acceptance remain later stories.

On 2026-11-06, all 14 PostgreSQL storage tests passed, including the five new
acceptance cases. Full discovery with PostgreSQL and Keycloak enabled passed all
46 tests. The first full run passed all 42 non-Keycloak tests but failed during
provider setup with HTTP 500; logs confirmed the known missing session-table
fault. After recreating development Keycloak from its existing realm import, the
full suite passed. Python syntax, local documentation links, and Git whitespace
checks also passed. Test schemas and their job/intent rows were cleaned up.

## US-005.1: initial target policy

`unit/test_target_policy.py` exercises the default-deny exact-origin allowlist,
scheme/host/port distinctions, case/default-port/terminal-dot normalization,
invalid configuration, public IP literals, alternate loopback representation,
restricted IPv4/IPv6 categories, IPv6 mapping/translation, mixed DNS answers,
resolution failures, and dual-family resolver behavior.

`integration/test_target_policy.py` uses signed JWTs and a test-only `/scan` route
with the real `require_allowed_submission` dependency. It verifies allowed public
targets, uniform HTTP 403 without handler execution, no DNS for invalid identity
or team context, schema HTTP 422, and startup configuration validation without DNS.
The shared `post_json` helper accepts optional authorization and extra headers;
existing submission-schema tests retain their behavior.

Run focused checks with no network:

```sh
docker run --rm --network none --read-only -v "$PWD:/workspace:ro" -w /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_target_policy tests.integration.test_target_policy tests.unit.test_submission_schema tests.integration.test_submission_validation -v
```

Every resolver response is controlled or mocked; public IPs in fixtures are data,
not contacted systems. The tests create no permanent jobs and launch no scanner.
This covers initial validation only. Redirect/crawl enforcement and the isolated
private fixture exception remain US-005.2/US-005.3. See
[Initial target policy](../docs/target-policy.md) for the security boundary.

On 2026-11-06, all 18 focused target-policy and submission tests passed with
network access disabled. Full discovery with PostgreSQL and Keycloak enabled
passed all 59 tests, including the final normalization and transition-address
cases. The first full run passed all 55 non-Keycloak tests but provider setup
returned HTTP 500; logs confirmed the existing missing session-table fault.
Recreating development Keycloak from its realm import restored token issuance
and the subsequent full suite passed. Changed Python syntax, local documentation
links, and Git whitespace checks passed. No target was contacted or scanned.

## US-003.2: production asynchronous acceptance

`integration/test_scan_acceptance.py` uses the production `POST /scan` route,
signed fixture tokens, real authorization and target policy, and PostgreSQL
schemas migrated and removed separately for each test. Controlled DNS answers
are fixture data; no target is contacted and no scanner is launched.

Coverage includes the generated OpenAPI response/security schema; HTTP 202 with
UUID, scan type, and the pending acceptance snapshot; committed job/intent and
verified ownership; 401/403/422 without unintended records; safe 503 and rollback
on an injected pre-commit failure; commit-before-response ordering; and persistence
after replacing the application object and connection pool. Replacement is not a
full process-restart demonstration. Retrieval UUID/unknown-job tests, dispatch,
execution, benchmarking, and full documented examples remain later work.

Run focused checks from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_scan_acceptance -v
```

Use the full-suite PostgreSQL/Keycloak command above for regression verification.
Without PostgreSQL opt-in, the six database tests skip explicitly; the OpenAPI
check runs offline. Mounted source tests verify the implementation without
rebuilding or replacing the running API image. No permanent demonstration jobs
are created. A connection lost during actual commit can leave the outcome
uncertain; the rollback test injects failure before commit and does not claim
otherwise.

On 2026-11-09, all seven focused checks and all 66 tests with PostgreSQL and
Keycloak enabled passed. The initial full run passed all 62 non-Keycloak tests
but provider setup returned HTTP 500. Logs showed the known missing-table fault
in Keycloak's development database. Recreating only that container from the
existing realm import restored token issuance; development sessions, signing
keys, and unpersisted console changes were reset. Python syntax, local
documentation links, and whitespace checks passed. The running API image was
not rebuilt or replaced.


## US-009.1: team-scoped status retrieval

`integration/test_job_retrieval.py` adds one offline OpenAPI check and six real
PostgreSQL tests. Each database test migrates its own disposable schema and
removes it afterward, restoring dependency overrides. Signed fixture tokens
exercise real authentication/team rules. The POST-to-GET test uses controlled
target resolution, and retrieval performs no DNS or target requests.

Checks cover owning-team and teammate access, two-way team isolation, complete
unknown/cross-team 404 response equivalence, malformed UUID 422 and 401/403 before
repository lookup, explicit multi-team selection, fresh persisted status, safe
503 without database detail, and the initial three-field public response. The
running fixture is written directly in a test; no scanner or worker runs.
Retrieval creates no jobs or dispatch intents. Lifecycle timestamps/outcomes
remain US-009.2, and complete polling integration remains US-009.3.

Run focused checks from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_job_retrieval tests.integration.test_scan_acceptance -v
```

Use the full-suite PostgreSQL/Keycloak command above for regression verification.
Without the PostgreSQL flag, the six retrieval database tests skip explicitly;
the OpenAPI check still runs. Mounted source tests do not rebuild or replace the
running API image, and create no permanent demonstration records.

On 2026-11-09, all 14 focused retrieval/submission checks and the full 73-test
suite with PostgreSQL and Keycloak enabled passed. The initial full run passed
all 69 non-Keycloak tests but provider setup returned HTTP 500. Logs confirmed
the known missing-table fault in Keycloak's development database. Recreating
only that container from its existing realm import restored token issuance;
development sessions, signing keys, and unpersisted console changes were reset.
The subsequent full run passed. Changed Python syntax, local documentation links,
and whitespace checks passed. The running API image was not rebuilt or replaced.


## US-008.1: guarded lifecycle transitions

`integration/test_job_lifecycle.py` adds seven real PostgreSQL checks. Each test
migrates and removes its own unique schema. Simultaneous operations use separate
sessions in two threads synchronized before execution; each commits independently.

Checks cover UTC timestamps and ownership preservation, one winning start,
wrong-worker rejection, competing terminal outcomes, repeated writes preserving
completed/failed/cancelled rows, a pending-failure/start race, invalid inputs and
missing jobs, and caller rollback plus database consistency failure. Outcome
objects and cancelled/running states are fixtures. No scanner, worker loop,
queue publisher/consumer, or permanent demonstration records are created.

Run focused checks from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_job_lifecycle -v
```

Use the full-suite PostgreSQL/Keycloak command above for regressions. Without
`RUN_POSTGRES_TESTS=1`, all seven lifecycle tests skip explicitly. Mounted source
tests do not rebuild or replace the running API image. See
[Guarded job lifecycle](../docs/job-lifecycle.md) for caller transaction ownership,
session refresh requirements, worker identity assumptions, and the limits of
application guards. Leases/recovery and semantic result/error validation remain
subsequent stories.


On 2026-11-09, all seven focused lifecycle tests passed, and the final full suite
passed all 80 tests with PostgreSQL and Keycloak enabled, including the final
pending-failure and cancellation-request guard assertions. The initial full run
passed all 76 non-Keycloak tests but provider setup returned HTTP 500. Logs
confirmed the known missing-table fault in Keycloak's development database.
Recreating only that container from its existing realm import restored token
issuance; development sessions, signing keys, and unpersisted console changes
were reset. The subsequent full run passed. Changed Python syntax, local
documentation links, and whitespace checks passed. No permanent demonstration
records were created and the running API image was not rebuilt or replaced.
