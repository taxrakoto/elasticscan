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
