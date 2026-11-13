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


## US-003.3: published submission contract

`integration/test_scan_acceptance.py` checks the generated production OpenAPI
request and response schemas, bearer security, optional team selector, and error
metadata. A PostgreSQL-backed test reads the documented request example from that
schema, sends a signed multi-team fixture token with explicit team selection, and
verifies HTTP 202, the response model, saved ownership, parameters, and atomic
job/intent creation. DNS and signing-key responses are controlled; real token
verification, authorization, target policy, and persistence remain active.

Run the focused suite from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_scan_acceptance -v
```

Use the full PostgreSQL/Keycloak discovery command above for regressions. Without
`RUN_POSTGRES_TESTS=1`, database tests skip explicitly; OpenAPI checks still run.
Test schemas are removed afterward. No scanner or permanent demonstration records
are created; mounted tests do not rebuild or replace the running API image.
See [Submission contract](../docs/submission-contract.md) for examples and limits.


## US-006.2: isolated SNS/SQS routing

`integration/test_queue_routing.py` adds three opt-in LocalStack checks: queue and
subscription configuration, restoration of changed subscription attributes without
duplicates, and actual attribute-based routing with raw body delivery. Unrelated
and absent routing attributes must not arrive during a 15-second observation
window. Matching duplicate deliveries are accepted; body/attribute mismatches are
diagnostic fixtures, not application job contracts.

The runner refuses nondefault SNS/SQS endpoints before creating resources. Each
test provisions unique topics/queues and registers cleanup, leaving application
queues untouched. It uses the real local SNS/SQS APIs with dummy credentials, not
mocked clients. These checks inspect policy configuration but do not establish
real AWS IAM enforcement. Without `RUN_LOCALSTACK_TESTS=1`, all three skip.

Focused command with LocalStack healthy:

```sh
docker compose run --rm --no-deps -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_queue_routing -v
```

Full PostgreSQL/Keycloak/LocalStack regression command:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_KEYCLOAK_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

These routing-only tests do not verify application publication; US-006.1 below
adds that check. SNS-outage recovery (AC-007), worker consumption, scanner
execution, and real AWS integration remain separate checks. See
[local routing configuration](../docs/local-development.md#us-0062-dedicated-queue-routing)
and [Publish–Subscribe](../docs/design-pattern/publish-subscribe.md).


## US-006.1: versioned publication and dispatcher

`fixtures/contracts/v1/job-message.json` is a shared actual message body, with
`schema_version`, job UUID, `scan_type=zap`, and validated URL parameters.
`unit/test_job_message.py` verifies round-trip serialization and invalid input.
`unit/test_dispatcher.py` verifies discovered-job publication, stop handling, and
safe waiting on database failure. Offline checks need no network:

```sh
docker run --rm --network none --read-only --mount "type=bind,src=$PWD,dst=/workspace,readonly" --workdir /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_job_message tests.unit.test_dispatcher -v
```

`integration/test_publication.py` migrates/removes one isolated PostgreSQL schema
per test. It verifies versioned content, matching SNS attributes, acknowledgment
and UTC timestamp persistence, unchanged pending job, safe failure/missing-ack
codes, invalid stored version, committed claims and competitor exclusion,
stale-owner rejection, missing/suppressed intent exclusion, automatic discovery,
and bounded automatic recovery after failure (US-006.3). Schema cleanup runs on
failures too.

Its LocalStack check provisions and removes unique resources, requires the
existing default emulator endpoints, and starts the real dispatcher entry point
in a child process. Only its database engine factory is injected to confine
queries to the disposable schema. US-006.3 first starts it with an unreachable
local SNS endpoint and checks its committed failure. A new process restores
LocalStack access, discovers the same saved job and publishes it without resetting
the attempt count. Both handle SIGTERM and remove their liveness marker. The test
compares received queue JSON with the shared contract and allows duplicates. Application queues and
jobs are untouched. Without service flags, applicable tests explicitly skip.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_job_message tests.unit.test_dispatcher tests.integration.test_publication tests.integration.test_queue_routing -v
```

Run the full PostgreSQL/Keycloak/LocalStack suite using the full regression command
in the US-006.2 section. The new publisher plus existing attribute-filter tests
cover local FR-015/AC-003 publication/routing. Failure retention contributes to
FR-014/AC-007; US-006.3 adds automatic recovery within the retry budget. Real AWS
IAM, worker consumption and full end-to-end execution remain separate work. See [Job publication](../docs/job-publication.md).


## US-006.3: bounded publication recovery

The dispatcher unit tests verify exponential delay/jitter bounds, the cap and
discovered-job calls without the first-attempt restriction. PostgreSQL tests cover:

- No premature retry, then publication of the saved job after recovery using a
  fresh connection pool.
- Five total attempts, a committed `exhausted` outcome and preserved pending job
  and unpublished/unsuppressed intent; no sixth call through discovery or direct
  publication.
- Active-claim exclusion, expired-claim recovery and interrupted-final-attempt
  exclusion at the budget boundary.
- Invalid saved-message exclusion without a repeated validation loop.
- A failed finalization transaction after mocked SNS success, followed by identical
  republication after simulated lease expiry. Duplicate publication is permitted.
- Safe log fields containing attempt count, next retry time and classified error.

Tests advance due/lease times only in their disposable schemas. The LocalStack
process test uses actual network clients: SNS access is refused through an
unreachable local endpoint, then restored by restarting with the emulator endpoint.
The same persisted job reaches its isolated ZAP queue. Shared LocalStack services,
application jobs/queues and the running dispatcher are not modified. Cleanup
removes temporary schemas, topics and queues, including on test failures.

Focused command from `elasticscan/`:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_dispatcher tests.unit.test_job_message tests.integration.test_publication tests.integration.test_queue_routing -v
```

Regression command with Keycloak integration explicitly left disabled:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

Without service flags, corresponding integration tests skip. These checks cover
FR-014/AC-007 publication recovery and existing FR-015/FR-016/AC-003 routing;
existing HTTP/storage tests cover failed initial acceptance and rollback.
No real AWS, scanner execution, proactive alerting or arbitrary-duration outage
recovery is claimed. See [Job publication](../docs/job-publication.md) for code
defaults, exhaustion inspection and operator recovery boundaries. Source-mounted
tests do not deploy changes to running application containers.


US-006.3 verification passed all 25 focused checks. Full discovery with PostgreSQL
and LocalStack passed 103 tests and explicitly skipped four Keycloak tests (107
discovered); Keycloak was left unchanged. Changed Python syntax, 64 local
Markdown links/anchors and Git whitespace checks passed. The documented inspection
SQL identified due, exhausted and invalid records and excluded published records
in a disposable migrated schema. Publication schemas and publication/routing
LocalStack topics/queues were confirmed removed. The cleanup inspection script
was corrected to close SDK clients explicitly after an initial context-manager
compatibility error; its final check passed. No application image/container was
rebuilt or replaced, and no scan ran. There were no material implementation
changes from the validated preview.


## US-007.1: worker validation and adapter handoff

`unit/test_zap_worker.py` rejects malformed JSON, unsupported/noninteger versions,
missing fields, invalid UUIDs/scan types/URLs and unknown fields before storage
or adapter access. `integration/test_zap_worker.py` adds six PostgreSQL tests
using isolated migrated schemas and the shared version-one message fixture.

Coverage includes missing/inconsistent jobs without mutation, complete parameter
matching, committed running state visible in an independent session before adapter
invocation, ownership preservation, terminal/cancellation exclusion, competing
deliveries and redelivery, precommit rollback with no invocation, and propagated
adapter exceptions leaving running state. A recording adapter replaces ZAP;
no target is contacted, queue consumed or permanent job created.

Focused command from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_zap_worker tests.integration.test_zap_worker tests.unit.test_job_message tests.integration.test_job_lifecycle -v
```

Use the US-006.3 PostgreSQL/LocalStack regression command above for full discovery
with Keycloak tests explicitly disabled. Without PostgreSQL opt-in, database
tests skip. Source-mounted tests do not rebuild or replace application containers.
See [Adapter](../docs/design-pattern/adapter.md) for the callable contract,
decisions and later execution/queue/recovery acceptance boundaries.

On 2026-11-11, all 16 focused checks passed. Full PostgreSQL/LocalStack discovery
passed 110 tests with four Keycloak tests explicitly skipped (114 discovered);
Keycloak remained unchanged. Syntax checks passed for all four new Python files,
27 local documentation links/anchors and fenced blocks passed, and Git whitespace
checks passed. Worker test schemas were confirmed removed. The final diff matched
the approved scope. No real scanner ran or application image/container was rebuilt
or replaced. Full AC-018 execution acceptance remains subsequent work.


## US-014.1: execution claims and leases

`integration/test_execution_leases.py` adds eight tests against real migrated,
disposable PostgreSQL schemas. Checks cover simultaneous claims and terminal
writes, token/worker ownership, positive configurable lease duration, UTC times,
unchanged job identity, renewal without shortening expiry, expired/missing-token
and stale-token rejection, rollback of claims/renewal/completion, invalid inputs,
unknown jobs, pending cancellation and current-clock checks in an older transaction.
Expiry and replacement attempts are set directly as fixtures; there is no recovery
loop. Existing worker tests also verify the adapter receives the committed claim
token and that competing validated deliveries invoke it once.

Focused command from `elasticscan/`, with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_execution_leases tests.integration.test_job_lifecycle tests.integration.test_zap_worker tests.unit.test_zap_worker tests.unit.test_job_message -v
```

Run the US-006.3 PostgreSQL/LocalStack full-discovery command for regressions, with
Keycloak tests left disabled. Without PostgreSQL opt-in these database tests skip.
Test schemas are removed on completion, including failures. Tests launch no real
scanner or permanent jobs and do not replace running application containers.
The existing schema is reused without a migration. These checks support AC-006;
full AC-008 queue visibility/acknowledgment and bounded recovery remain
US-014.2/US-014.3. See [Execution claims and leases](../docs/execution-leases.md)
for callable contracts, transaction and clock rules, provisional defaults and
remaining periodic-renewal/execution boundaries.

On 2026-11-11, all 24 focused checks passed, including the eight new lease tests.
Full PostgreSQL/LocalStack discovery passed 118 tests and explicitly skipped four
Keycloak tests (122 discovered); Keycloak remained unchanged. Syntax checks passed
for all five affected Python files, 40 local documentation links/anchors and fenced
blocks passed, and Git whitespace checks passed. Lease and worker test schemas
were confirmed removed. The clock test was tightened within the approved behavior
to distinguish current time from transaction-start time, and ownership/input
assertions were strengthened. No material scope deviation occurred. No scanner ran
or application image/container was rebuilt or replaced. Periodic renewal,
queue visibility/acknowledgment and bounded recovery remain subsequent work.


## US-005.2: single-origin application phase

Deployment configuration now accepts zero or one exact origin. More than one
entry is a configuration error, while an empty list remains default deny. The
existing target-policy tests cover that configuration restriction.

Seven unit tests in `unit/test_scanner_scope.py` cover normalized origin/path
matching, out-of-origin redirect/link candidates, IPv4/IPv6 literals, current
private/mixed resolution rejection, empty/multiple configuration, context setup
and setup errors. They use recording ZAP clients; no real redirect or crawl is
performed. Three additional PostgreSQL worker tests verify changed origin,
private DNS answers and empty policy never claim or mutate the saved job or
invoke its adapter. The existing worker success test checks scope metadata is
forwarded after the claim commits. DNS responses are controlled test data.

Focused command from `elasticscan/` with PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_scanner_scope tests.unit.test_target_policy tests.unit.test_zap_worker tests.integration.test_zap_worker tests.integration.test_target_policy -v
```

Use the US-006.3 PostgreSQL/LocalStack full-discovery command for regression checks
with Keycloak disabled. Tests create and remove disposable schemas, contact no
target and do not deploy running application containers. Real ZAP/API runtime
verification and redirect/link crawling remain US-007.2; connection-time DNS and
enforceable AWS outbound restrictions remain deployment work. US-005.2 stays open
because its original NFR-007/AC-010/AC-018 requirements are not fully verified.
See [Single fixed scanner origin](../docs/scanner-scope.md) for the callable
contract, configuration, reused patterns and limits.

On 2026-11-11, all 30 focused checks passed. Full PostgreSQL/LocalStack discovery
passed 128 tests and explicitly skipped four Keycloak tests (132 discovered);
Keycloak remained unchanged. Syntax checks passed for seven affected Python files,
44 local documentation links/anchors and fenced blocks passed, and Git whitespace
checks passed. Worker test schemas were confirmed removed. No target was contacted
or real scanner started, and no application image/container was rebuilt or
replaced. The private `.env` was unchanged. The final diff matched the approved
single-origin application phase, with no material deviation. Real ZAP redirect/
crawl/runtime verification and enforceable network restrictions remain pending;
the backlog status and successful session history were not changed by these checks.


## US-007.2: bounded baseline execution

`unit/test_baseline.py` checks the fixed baseline command, separate crawl/total
budgets, raw finding-related exit code retention, missing credential options,
process-group timeout cleanup, temporary-file removal, scope-before-access and
passive/active execution checks. These tests launch no scanner.

`integration/test_baseline.py` opts in with `RUN_ZAP_BASELINE_TESTS=1` and requires
the worker image and isolated fixture setup. It runs actual ZAP against a public-
format address assigned only to a container-local loopback alias with no external
network. It checks in-origin discovered crawling, passive queue completion,
active-scan count zero, out-of-origin link exclusion and a one-second startup
deadline. Findings and raw exit codes are not normalized into persisted outcomes.

```sh
docker compose build zap-worker
sh scripts/verify_zap_baseline.sh
```

The helper uses root/NET_ADMIN only to prepare the disposable fixture, then drops
to `zap`. No production private-address exception, gateway, application secrets or
real external target is involved. The host source mount is read-only. Import and
file-inventory smoke checks must separately run against the built image without
a source mount, so packaging omissions cannot be hidden by tests.

Run PostgreSQL/LocalStack regression discovery using the existing US-006.3 command
with Keycloak disabled. Real-scanner tests skip explicitly outside the helper.
The current worker remains idle and discards adapter output when called through
`handle_message`; lease renewal, queue acknowledgment, result classification and
terminal persistence remain subsequent work. See
[ZAP baseline](../docs/zap-baseline.md) for budget semantics, cleanup guarantees,
raw-output handling, the existing single-origin scope and remaining acceptance.

On 2026-11-11, the final isolated real-ZAP run passed both tests, including actual
process-group and temporary-directory cleanup after completion and timeout.
Fourteen focused baseline/scope/worker unit checks passed; final PostgreSQL/
LocalStack discovery passed 134 tests with four Keycloak and two separately-run
real-ZAP tests skipped (140 discovered). Keycloak remained unchanged. The narrowed
worker image built successfully, and final imports/content checks passed without
mounting source: API routes/main, dispatchers, services, migrations, tests and
docs were absent. Five Python files, shell syntax, 44 local documentation links/
anchors, fenced blocks and Git whitespace checks passed. Worker test schemas and
disposable scanner containers were confirmed removed.

Initial real runs exposed packaged wildcard API defaults, IPv6 loopback/special
API Host handling and the Python client's `allow_redirects` argument. These were
corrected within the approved local-only baseline behavior; earlier blocked test
containers were stopped and removed. The fixture uses a standard-library Linux
ioctl because the base image has no `ip` tool; no extra package was added. No
gateway, external target or private-policy bypass was introduced. No material
scope deviation occurred. The worker image was built but running application
containers were not replaced, and the private `.env` was unchanged. Comprehensive
redirect/network enforcement, live consumption/lease renewal, normalized outcomes
and terminal persistence remain subsequent work.


## US-007.3: ZAP outcome interpretation

`unit/test_zap_outcomes.py` tests finding-related exit codes 0/1/2, grouped high/
medium/low counts, informational exclusion, empty alerts, malformed reports,
execution evidence, safe timeout/execution/output failures and propagation of
unexpected programming errors. These checks use no real scanner or database.

```sh
docker compose run --rm --no-deps -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_zap_outcomes tests.unit.test_baseline tests.unit.test_scanner_scope tests.unit.test_zap_worker -v
docker compose build zap-worker
sh scripts/verify_zap_baseline.sh
```

The real-baseline integration test now also requires actual findings to normalize
to `completed`. The existing real timeout test checks cleanup; expected exception
classification is covered separately by unit tests. Reuse the PostgreSQL/
LocalStack full-discovery command above with Keycloak disabled for regressions.
Check interpreter imports and the explicit content inventory in the built worker
image without a source mount. See [ZAP outcomes](../docs/scanner-outcomes.md).

This is reusable interpretation only: no terminal database write, queue polling,
API result change, periodic lease renewal or new network enforcement is added.

On 2026-11-11, all 23 focused checks passed. PostgreSQL/LocalStack regression
discovered 149 tests: 143 passed and six skipped (four Keycloak and the two
real-ZAP tests run separately). Both isolated real-ZAP tests passed, including
actual findings normalizing to completed and process/temporary-file cleanup on
timeout. The worker image built and passed imports/content checks without a
source mount. Syntax for four Python files, shell/Compose configuration, 42 local
documentation links/anchors, fenced blocks and Git whitespace checks passed.
Keycloak and running application containers were unchanged. No external target
was scanned, and no material deviation from the approved proposal occurred.


## US-008.2: atomic result summaries

`integration/test_result_summaries.py` uses disposable migrated PostgreSQL schemas.
It verifies zero/nonzero normalized findings, strict count/token validation,
identity and UTC timestamps, rollback after a real database statement failure,
visibility before commit, team-scoped retrieval after replacing application
connection resources, stale/expired/wrong ownership, duplicates and competition.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_result_summaries tests.integration.test_job_lifecycle tests.integration.test_execution_leases tests.integration.test_job_retrieval -v
docker compose build zap-worker
python3 -B scripts/verify_result_summaries.py
```

Without PostgreSQL opt-in the new database tests skip. The actual-adapter summary
test also skips unless the verification helper supplies `REAL_ZAP_RESULT_JSON`.
That helper first runs the two actual scanner tests in the existing network-none
fixture, then passes only validated normalized counts to the PostgreSQL tests in
a separate container. This proves persistence of actual adapter output without
giving the scanner access to the database or an external target. Scanner reports
are not exported. A fresh pool/session factory simulates application resource
replacement; PostgreSQL is not restarted.

Use the existing PostgreSQL/LocalStack regression command with Keycloak disabled.
Check built worker imports without a source mount. No new dependency, migration,
whole-app Docker copy, queue consumption, automatic lease renewal or HTTP result
serialization is introduced. See [Atomic result summaries](../docs/result-summaries.md).

On 2026-11-11, focused discovery passed 27 tests with the actual-summary test
explicitly skipped until the helper run (28 discovered). The two-stage helper
then passed both isolated real-ZAP tests and all six result-summary tests,
including persistence and retrieval of actual normalized scanner counts. Full
PostgreSQL/LocalStack regression passed 148 tests with seven expected skips
(four Keycloak, two separately run real-ZAP tests and the separately run actual-
summary test; 155 discovered). The worker image built and passed completion
imports/content checks without a source mount. Syntax for four Python files,
shell/Compose configuration, 42 documentation links/anchors, fenced blocks and
Git whitespace checks passed. Keycloak and running application containers were
unchanged. No external target was scanned or material scope deviation introduced.

## US-008.3: safe failure persistence

The additions to `integration/test_result_summaries.py` persist controlled scanner
exceptions through interpretation and safe failure recording. They verify fixed
code/message objects, replacement of unsafe supplied messages, null unused fields,
UTC/identity preservation, replacement connections, team isolation, validation,
claim fencing, terminal immutability, statement-error rollback and competing
failure/completion or exhaustion/claim transitions. No real scanner is launched.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_result_summaries tests.integration.test_job_lifecycle tests.integration.test_execution_leases tests.integration.test_job_retrieval tests.unit.test_zap_outcomes -v
docker compose build zap-worker
docker run --rm --network none --entrypoint python3 elasticscan-zap-worker:local -B -c 'from app.repositories.lifecycle import fail_job, fail_exhausted_job; from app.core.failure import safe_error; assert safe_error("scanner_timeout")["code"] == "scanner_timeout"'
```

Use the US-006.3 PostgreSQL/LocalStack discovery command for regressions, with
Keycloak disabled. PostgreSQL checks skip without opt-in; real-ZAP and actual-
summary checks remain separately opt-in. Disposable schemas are removed after
checks. Imports in the built image must work without a source mount. See
[Safe failures](../docs/safe-failures.md) for scope and compatibility limits.

Stored safe errors are verified; HTTP serialization and live-worker AC-005 remain
pending. No running application container, private configuration, migration or
dependency is changed by this story.

On 2026-11-12, focused discovery passed 42 checks with the actual-scanner-summary
check explicitly skipped (43 discovered). Full PostgreSQL/LocalStack discovery
passed 154 checks with seven expected skips (161 discovered): four live Keycloak,
two isolated real-ZAP and one actual-scanner-summary test. No real scanner was
launched in this story; existing real-scanner cleanup tests were not rerun.
The worker image built and passed failure-function imports and narrowed file
inventory checks without a source mount. Three changed Python files passed syntax
checks; 47 local documentation links/anchors, fenced blocks and Git whitespace
checks passed. The diff matched the approved reusable-function scope. Keycloak
and running application containers were unchanged.

Result-summary, lifecycle and execution-lease disposable schemas were confirmed
removed after verification.


## US-009.2: lifecycle response serialization

`retrieval_contract.py` defines version-one examples for pending, running,
completed, failed and proposed cancelled responses. Submission fixtures do not
contain these examples and remain unchanged. `unit/test_retrieval_schema.py`
checks explicit nulls, UTC conversion, valid state shapes, strict integer counts,
safe error reconstruction and failures before start. No external services are
needed for these unit checks.

`integration/test_job_retrieval.py` retains US-009.1's authorization and status
checks and adds production HTTP responses backed by real guarded PostgreSQL
claims, result writes and safe failures. It covers two-team isolation for every
state, fresh connections, ZAP normalizer exit 0/1/2, exhausted pending failures,
legacy message reconstruction and generic 503 for incompatible saved outcomes.
The cancellation row is a test fixture; a timeout is an injected adapter failure.
Neither proves live cancellation or a real scanner timeout.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_retrieval_schema tests.integration.test_job_retrieval -v
```

`python3 scripts/verify_result_summaries.py` runs isolated real ZAP verification,
then both result-persistence tests and the opt-in HTTP test with those actual
counts. The normal suite explicitly skips that case without REAL_ZAP_RESULT_JSON.
Use the standard full regression command with PostgreSQL/LocalStack enabled;
Keycloak remains separately opt-in. See the
[Retrieval contract](../docs/retrieval-contract.md) for acceptance boundaries.


## US-019.2: partial local integration slice

`integration/test_local_slice.py` joins real production ASGI submission/retrieval,
Keycloak authentication and team resolution, migrated temporary PostgreSQL
storage, production publication to isolated LocalStack SNS/SQS, worker claims,
outcome interpretation and guarded persistence. Scanner output/timeout and the
orchestration joining the worker outcome to persistence are test-only.

```sh
python3 -B scripts/verify_local_slice.py
```

The helper requires the existing API image and healthy PostgreSQL, LocalStack
and Keycloak. It runs six checks in a disposable source-mounted container. The
checks cover two-team success and terminal replay, safe simulated timeout,
rejected API requests without records, routing exclusion followed by accepted
publication, injected publication failure/recovery and invalid worker deliveries.
All four flags (`RUN_LOCAL_SLICE_TESTS`, `RUN_POSTGRES_TESTS`,
`RUN_LOCALSTACK_TESTS`, `RUN_KEYCLOAK_TESTS`) must equal `1`; normal discovery
skips the class otherwise. The dedicated module entry point rejects skipped
runs and requires exactly six executed tests.

Schemas/topics/queues use unique test prefixes and register cleanup on ordinary
failures. No default queue is consumed, application schema migrated, target
contacted or real scanner launched. No running service is rebuilt, recreated or
repaired. Identity-provider login sessions are created by token acquisition.
See the [partial local-slice guide](../docs/local-slice.md) for exact simulated
boundaries and the remaining US-005, budget, audit, live-worker and AWS work.
US-019.2 stays open; full AC-012/AC-022 acceptance is not claimed.


On 2026-11-12, all six checks passed through the normal
`scripts/verify_local_slice.py` entry point, using real Keycloak JWTs, migrated
PostgreSQL schemas and isolated LocalStack resources. All four existing live
Keycloak authentication/team/security checks also passed separately.

The first live attempt failed before tests/migrations because Keycloak returned
HTTP 500 with its existing H2 missing `OFFLINE_USER_SESSION` table fault. The
user authorized finishing this task without further approval, including service
repair. Recreating only Keycloak restored token acquisition, resetting its
in-memory sessions, signing keys and nonpersisted console changes. Other running
application services were not rebuilt/recreated; the private `.env` was unchanged.
Before repair, a separate diagnostic run of all six flows passed with ephemeral
RSA fixture tokens. That diagnostic was not counted as live Keycloak acceptance.

PostgreSQL/LocalStack regression discovery ran 177 tests: 163 passed and 14 were
explicitly skipped (four live Keycloak and six local-slice checks subsequently
passed separately; two real-ZAP and two actual-scanner-result persistence/HTTP
checks were not run). No real scanner was launched or target contacted. Both new
Python files passed syntax checks; 58 local documentation links/anchors, six
Markdown fence checks and Git whitespace checks passed. Cleanup verification
confirmed no local-slice schemas, topics or queues remained. An initial read-only
cleanup command used unsupported SDK context managers; its corrected command
passed without changing task code.

Existing migration `0001_create_transactional_job_storage.py` was applied only
to disposable test schemas, which were removed afterward. No migration was
created or applied to the application schema. The Adapter and Dependency
Injection patterns were reused and documented. US-005 remains unchanged and
US-019.2 stays open: real authorized scanning and deployed worker orchestration
are still pending. The only scope extension was the user-authorized Keycloak
repair; task code matched the displayed implementation preview.


## US-014.2: delivery and US-014.3: partial recovery

`unit/test_worker_runtime.py` verifies timing validation, shared parsing,
interruption propagation, unchanged legacy timeout waits and shutdown before
polling. `integration/test_worker_delivery.py` uses disposable migrated PostgreSQL
schemas and isolated LocalStack SNS/SQS resources. It exercises real visibility
and lease renewal, durable completed/failed outcomes, terminal redelivery,
persistence rollback with queue recovery, renewal uncertainty, cooperative
shutdown, expired-claim failure, stale writers, cancellation-request preservation,
competing consumers and the actual queue loop. Controlled scanner adapters contact
no targets. The existing claim-concurrency tests remain part of verification.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_worker_runtime tests.unit.test_baseline tests.unit.test_zap_worker tests.integration.test_worker_delivery tests.integration.test_zap_worker tests.integration.test_execution_leases -v
docker compose build zap-worker
sh scripts/verify_zap_baseline.sh
```

Without both database/emulator flags the new integration class skips explicitly.
Full discovery should enable PostgreSQL/LocalStack and separately opt in to live
Keycloak/local-slice checks when healthy. The real-ZAP helper now has three tests:
completion with findings, total timeout, and stop-event interruption. Its existing
fixture verifies process groups and temporary directories are removed; it has no
external network access. Worker packaging must import without a source mount,
exclude API/dispatcher/tests/docs/migrations and run under the existing nonroot
user. No application worker or queue is activated by these commands.

Only existing migration 0001 is applied to temporary test schemas. Cleanup removes
the test-delivery topics/queues and test_delivery schemas. US-014.3 remains partial
and no budget/cancellation/deletion/AWS acceptance is inferred. See
[Worker delivery](../docs/worker-delivery.md) for exact behavior and limitations.


On 2026-11-12, 40 focused worker/baseline/claim/lease checks passed, including
all eleven new delivery integration tests and five new runtime unit tests.
The heartbeat test was then strengthened and passed separately with its scanner
held beyond both original windows (three-second lease and six-second visibility),
verifying continued ownership and hidden delivery after seven seconds.

Regression discovery passed 186 checks with five expected skips (191 discovered).
It included live Keycloak and the six existing authenticated local-slice checks.
Three skipped real-ZAP checks passed separately in the network-disabled fixture:
completion with actual findings, timeout and stop-event interruption, with
process-group and temporary-directory cleanup. The two existing actual-scanner
result persistence/HTTP checks were not rerun. Three additional delivery failure/
guard checks were added after that discovery and passed in the 40-check focused
run; no application implementation changed after the successful regression.

The first full discovery encountered HTTP 500 in both Keycloak-dependent class
setups. After explicit user approval, only Keycloak was recreated, restoring token
acquisition and resetting its volatile sessions/signing keys/console changes.
Other application containers were confirmed unchanged. The worker image built,
passed imports/narrow inventory checks without a source mount, and passed an
actual startup/PostgreSQL-timeout-options/cleanup smoke check with its queue loop
replaced so application deliveries were not consumed. Compose validation passed.

Eleven Python syntax checks, 85 local links/anchors, eight Markdown fence checks
and Git whitespace checks passed. Cleanup verified removal of worker delivery,
claim and lease schemas and delivery topics/queues. No migration, dependency or
private `.env` change was made. Existing migration 0001 was used only in temporary
test schemas. No worker was activated on application jobs or external target
contacted. Verification added five integration scenarios within the approved
files and strengthened the heartbeat assertion; the only operational scope
extension was the separately approved Keycloak repair.

The user accepted the scoped result on 2026-11-12. US-014.2 was marked Done and
the session was recorded outside the application repository. US-014.3 remains
partial, including conservative expired-claim failure rather than automatic
scanner reexecution. US-017.1, US-005, full cancellation/deletion recovery and
hard-crash acceptance remain pending.


## US-014.3 recovery and US-017.1 execution budgets

The recovery scheduler runs independently of an active scanner. Its interval and
batch size are validated configuration; the conservative execution-attempt cap
remains one. See [execution budgets](../docs/execution-budgets.md) for provisional
capacity settings, timeout semantics, shutdown and dependency/backlog limits.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_worker_runtime tests.unit.test_baseline tests.unit.test_zap_worker tests.integration.test_worker_delivery tests.integration.test_zap_worker tests.integration.test_execution_leases -v
docker compose build zap-worker
sh scripts/verify_zap_baseline.sh
```

The new process-kill test starts a separate Python worker handoff, waits for its
real PostgreSQL claim to commit, kills that process and waits for natural lease
expiry. Its scanner is simulated: no external target or real ZAP process runs in
that test. Further checks cover recovery batching, repeat/competition safety,
rollback and recovery while another scanner is busy. Existing checks retain
shutdown, cancellation-request preservation, missing records, stale writers,
acknowledgment ordering and permanent-error coverage. Unit tests verify the
provisional 900-second timeout default and environment override, recovery
configuration and recovery-loop storage failure/shutdown behavior.

The real-ZAP helper applies CPU/memory limits to its network-none fixture and
checks process-group and temporary-directory cleanup on completion, shortened
timeout and interruption. Shortened timeout testing plus default configuration
checks do not represent a literal fifteen-minute run. Full cancellation/deletion
APIs, tombstones, AWS acceptance and the US-017.3 API benchmark are separate.

On 2026-11-13, the focused suite passed 47 checks. One additional timeout-default/
override unit test then passed in full PostgreSQL/LocalStack regression discovery:
202 tests discovered, 187 passed, 15 skipped. Skips were four live Keycloak and six
local-slice checks (unaffected opt-in suites not run), three real-ZAP checks
subsequently passed separately, and two actual-scanner-result persistence/HTTP
checks not rerun. All three isolated real-ZAP checks passed under provisional
resource limits, including real findings and cleanup. No failed checks remained.

The worker image built and a disposable Compose run, without a source mount,
verified imports of the recovery scheduler/main/settings/adapter, the nonroot
user, narrowed inventory, and actual cgroup limits of 1 CPU and 2 GiB. Compose
configuration checks passed for defaults and overrides (0.5 CPU, 1 GiB, two
replicas), without displaying resolved private configuration. No application
container was replaced or worker activated on application jobs. No private `.env`,
dependency or migration was changed. Existing migrations were applied only to
disposable schemas. Source changes remain subject to result acceptance.


Final static checks passed for five changed/new Python files, shell syntax,
seven Markdown documents with balanced fences, 79 local documentation link
targets and Git whitespace. Cleanup checks confirmed no delivery/execution/worker
schemas or delivery queues/topics remained. All six existing service container
IDs remained unchanged. No material behavior, interface or scope deviation from
the preview occurred; timeout-default/override coverage was added within the
approved runtime test file. The user accepted this result on 2026-11-13 and authorized session recording
and marking US-014.3 and US-017.1 Done. Commit approval remains separate.


## US-015.1: worker failure classification and bounded retries

[Worker retries](../docs/worker-retries.md) defines the transient allowlist,
configuration, repeatable boundaries and distinction between operation attempts,
scanner attempts and queue delivery counts. The implementation wraps only
receive, fresh terminal confirmation and acknowledgment. It never retries the
scanner, claim/outcome transactions or uncertain renewal authority.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_worker_retry tests.unit.test_worker_runtime tests.unit.test_zap_outcomes tests.integration.test_worker_delivery tests.integration.test_execution_leases -v
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -e RUN_KEYCLOAK_TESTS=0 -e RUN_LOCAL_SLICE_TESTS=0 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
docker compose build zap-worker
```

Eight new unit tests cover classification, transient success, exact retry
exhaustion, original-exception preservation, permanent-error handling, safe logs,
shutdown, delay cap, single-attempt behavior, settings and consumer exhaustion.
Three new real PostgreSQL/LocalStack checks inject transient failures at the
production delete/confirmation boundaries and verify permanent scanner errors
are safely persisted once. The existing acknowledgment-exhaustion test now
asserts the configured call count and still verifies terminal replay without a
second scan. Faults and scanner adapters are injected; no network outage or real
scanner is exercised by those checks. Mocked retry waits avoid test delays.

The default is three total attempts and two/four-second retry waits. Expired or
uncertain claims still follow conservative recovery. Supervisor restart and
later operations get new retry budgets; this is not a durable lifetime retry
counter. US-015.2/US-015.3 retain full dead-letter/reconciliation acceptance.
No migration, dependency or public contract change is introduced.

On 2026-11-13, all 52 focused checks passed. The worker image built successfully
and imported/executed the new retry helper without a source mount or network,
under its existing nonroot user. The narrowed image inventory remained intact.
Real-ZAP checks were not rerun because scanner/cleanup code was unchanged; the
existing separately verified cleanup behavior is preserved. No application
container was rebuilt in place or activated on application deliveries, and the
private `.env` remained unchanged. The user accepted this result on 2026-11-13 and authorized session recording
and marking US-015.1 Done. Commit approval remains separate.

Regression discovery passed on 2026-11-13: 213 discovered, 198 passed and 15
explicitly skipped (four live Keycloak, six local-slice, three real-ZAP and two
actual-scanner-result persistence/HTTP checks). Those opt-in suites were not
rerun: this change affects worker retry boundaries, not authentication or scanner
execution. Six Python syntax checks, six Markdown fence checks, 68 local link
targets and Git whitespace checks passed. Cleanup verified no delivery/execution
schemas or delivery queues/topics remained; all six existing service container
IDs were unchanged. No failed checks or material deviations from the approved
preview remained. The accepted session entry and backlog status were updated outside this
repository after user acceptance. No commit has been created for this story.


## US-015.2: dead-letter configuration and safe observation

[Dead-letter delivery](../docs/dead-letter-delivery.md) documents configurable
source redrive, repaired queue attributes, retention and the bounded operator
command. It distinguishes job/scanner attempts from infrastructure retries and
source/DLQ receive counts. Observation temporarily hides samples for 30 seconds;
it does not delete, redrive, scan or edit database jobs.

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_dead_letters tests.integration.test_queue_routing tests.integration.test_worker_delivery -v
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -e RUN_LOCALSTACK_TESTS=1 -e RUN_KEYCLOAK_TESTS=0 -e RUN_LOCAL_SLICE_TESTS=0 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
docker compose build api
docker run --rm --network none --entrypoint python elasticscan-api:local -B -m app.maintenance.dead_letters --help
```

Tests use unique LocalStack topics/queues and disposable migrated PostgreSQL
schemas. The poison-message test publishes a correlated unsupported schema,
receives it three times through production consumer validation, verifies each
receive count, and observes service redrive to the DLQ. Safe logs include the job
ID hint, scanner construction never occurs, and the job remains pending with zero
attempts. The queue-repair test alters delivery count/retention/allow policy and
reprovisions with a five-receive bound, checking stable resource identity and
restored attributes. Existing subscription/routing checks remain unchanged.

Unit tests verify safe diagnostic fields, valid and malformed bodies, observation
limits, wrong/malformed queue policies, nondeletion/nonredrive, provisioning
validation and CLI client cleanup/exit codes. The command does not assert database
ownership or complete reconciliation; US-015.3 retains those requirements.

On 2026-11-13, all 29 initial focused checks passed. Three additional unit checks
for malformed allow policy and CLI paths then passed in full regression discovery:
223 discovered, 208 passed, 15 skipped. Skips were four live Keycloak, six
local-slice, three real-ZAP and two actual-scanner-result persistence/HTTP checks.
These unaffected opt-in suites were not rerun; scanner/authentication code was
unchanged. No failed checks remained. The source-ARN-list validation correction
and additional unit checks stayed within the approved files and behavior.

No application queue was consumed or reprovisioned. The shared-queue bootstrap
verification was not run; its configurable-bound expression is covered by syntax
review. Default-queue setup is applied only when deliberately reprovisioned by an
operator. Running services and private configuration remain unchanged. The user accepted
the result on 2026-11-13 and authorized session recording and marking US-015.2
Done. Commit approval remains separate.

Final API-image rebuild and network-none checks passed: the packaged CLI help,
maintenance imports/safe parsing, nonroot execution and absence of consumer-module
imports worked without source mounts. Compose validated delivery-bound wiring
for defaults and a five-receive override without displaying private configuration.
Six changed/new Python files passed syntax checks; six Markdown documents passed
fence checks, 69 local link targets resolved, and Git whitespace/final-diff checks
passed. Cleanup confirmed removal of delivery schemas and delivery/routing queues
and topics. All six existing application-service container IDs were unchanged.
No live queue attributes were applied and no external target was scanned.
