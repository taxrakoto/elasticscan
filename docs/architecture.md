# ElasticScan architecture

ElasticScan separates the HTTP API, PostgreSQL persistence, message dispatch,
and scanner execution into independent responsibilities. SNS routes messages to
scanner-specific SQS queues; workers execute scans and persist their outcomes.

The current implementation is a local bootstrap: the API exposes liveness and
OpenAPI, PostgreSQL and messaging infrastructure are provisioned, and Keycloak
provides development identities for two teams. The worker remains an idle
placeholder; US-006.1 adds publication and US-006.3 adds bounded dispatch recovery.
Job/dispatch storage and Alembic migrations are implemented in US-004.1; see [Database migrations](database-migrations.md).
US-001.1 implements trusted JWT validation and a reusable FastAPI authentication
dependency. US-002.1 adds a team-context dependency: bearer token → verified
claims → authorized `TeamContext` → future job handler. It validates configured
membership claims and an optional `X-Team-ID` selector; multi-team callers must
select a member team. See [Authentication](authentication.md) for the contract.
US-002.2 connects the context to `app/repositories/jobs.py`: the repository inserts
ownership from `TeamContext`, while the existing database trigger rejects later
ownership changes through object edits, bulk updates, or raw SQL. The caller
owns the transaction; the function flushes the insert without committing.
US-004.2 adds `app/services/acceptance.py`: validated submission and authorized
context → acceptance service → one job/dispatch-intent transaction → immutable
`AcceptedScan` result. The service owns its session and returns only after commit;
errors roll back the transaction and propagate to the caller. It makes no SNS
or scanner calls. US-003.2 connects production `POST /scan` to this service;
US-009.1 adds production team-scoped status retrieval.

US-005.1 adds `app/core/target_policy.py`. The production submission flow is verified
identity → authorized team → typed request → initial-target policy → durable
acceptance. The dependency checks exact scheme/host/port approval, then permits
only public addresses across all returned IPv4/IPv6 answers. Invalid targets
are denied before the handler. Startup validates the default-deny configuration
without DNS. See [Initial target policy](target-policy.md). The acceptance service
remains a trusted persistence operation; `app/api/routes/scans.py` explicitly uses
the policy dependency before invoking it. Redirect, DNS-change, and crawl-time egress
enforcement remain US-005.2.

## Durable HTTP acceptance (US-003.2)

`app/main.py` registers the scan router and creates one database engine and session
factory after startup configuration validation. Engine creation opens no database
connection. The lifespan's `finally` block disposes the pool at shutdown.
`app/api/dependencies.py` supplies the factory from application state; the
acceptance service owns each individual session and transaction. The synchronous
route runs through FastAPI's thread pool so database work does not block the
asynchronous event loop.

The route returns HTTP 202 only after `accept_scan` returns a committed
`AcceptedScan`. `ScanAcceptance` validates and documents the public UUID,
`scan_type: "zap"`, and fixed acceptance `status: "pending"`. It is a snapshot,
not a reread of current worker progress. The handler neither publishes messages
nor calls a scanner, and waits only for validation and durable persistence.

SQLAlchemy persistence failures return HTTP 503 with the safe detail
`Durable acceptance could not be confirmed`, without SQL or connection details.
Pre-commit failures roll back both records. A lost connection during commit may
leave an uncertain result; a 503 cannot guarantee that no record exists in that
case. Automatic retry/idempotency is not implemented; repeated submissions may
create additional jobs.

`tests/integration/test_scan_acceptance.py` exercises the production route with
signed test identities, controlled DNS, and migrated disposable PostgreSQL
schemas. It verifies committed acceptance, denial/validation without records,
rollback and safe errors, commit-before-response ordering, and persistence across
application/connection-pool replacement. This is not a full API-process restart
demonstration or a GET retrieval test. Full OpenAPI examples and documented
fixture-target verification remain US-003.3; process restart acceptance, dispatch,
and execution remain subsequent work.

## Design patterns

The [design-pattern catalog](design-pattern/README.md) records each pattern's
purpose, application to ElasticScan, implementation status, tradeoffs, and
verification requirements.

- [Transactional Outbox](design-pattern/transactional-outbox.md): save jobs and
  publication intent atomically, then publish through a background dispatcher.
- [Idempotent Consumer](design-pattern/idempotent-consumer.md): handle duplicate
  queue deliveries using persisted job claims and terminal-state protection.
- [Publish–Subscribe](design-pattern/publish-subscribe.md): route SNS publications
  to scanner-specific SQS queues independently of worker instances.
- [Dependency Injection](design-pattern/dependency-injection.md): supply verified
  identity and claim-mapping settings to the team authorization dependency.
- [Value Object](design-pattern/value-object.md): carry the resolved team and
  current caller together in an immutable `TeamContext`.
- [Repository](design-pattern/repository.md): insert validated jobs with explicit
  ownership mapping, leaving transaction orchestration to the caller.

The outbox's atomic database acceptance is implemented and covered by PostgreSQL
rollback and visibility tests. US-006.1 implements publication and US-006.3 adds
bounded publication retries and expired-claim recovery. The complete idempotent
consumer remains proposed feature behavior. Publish–subscribe
infrastructure and application publication are implemented locally; worker
consumption remains pending.
Documentation is not evidence that pending behavior is implemented.

See the [requirements](requirements.md) for the system workflow and acceptance
criteria, and [local development](local-development.md) for the verified bootstrap
configuration and startup instructions.


## Team-scoped status retrieval (US-009.1)

`GET /scan/{job_id}` in `app/api/routes/scans.py` receives a UUID, verified
`TeamContext`, and the existing injected session factory. The route owns one
read session and calls `find_team_job` in `app/repositories/jobs.py`. Its SQL
query includes both `job_id` and authorized `team_id`, so an inaccessible job is
never loaded for a later ownership check. The creator is not a read restriction:
another member of the owning team may read it. Multi-team callers must select
a verified membership through `X-Team-ID`.

`JobStatusResponse` in `app/schemas/retrieval.py` returns job identity, current
persisted status, UTC timestamps, nullable severity counts, canonical safe errors
and cancellation metadata (US-009.2). It is constructed while the session is open.
Unlike POST acceptance, this is a fresh database read rather than a fixed pending
snapshot. Ownership, target parameters and worker claim details are excluded.
The model validates lifecycle nullability, integer counts, timezone awareness and
cancellation metadata. Unrecognized errors or invalid saved data return safe 503.
See the [Retrieval contract](retrieval-contract.md) for all fields and examples.
Cancellation is fixture-only; live orchestration and complete polling are pending.

Missing/invalid authentication returns HTTP 401; invalid or ambiguous team context
returns HTTP 403. Authenticated authorized callers receive HTTP 422 for malformed
UUIDs, or identical HTTP 404 `Job not found` responses for unknown and cross-team
jobs. SQLAlchemy read failures return HTTP 503 with
`Job retrieval is temporarily unavailable`, without database details. Retrieval
does not mutate jobs, revalidate target policy, resolve target DNS, publish
messages, or execute scans. Changing the submission allowlist does not prevent
an authorized team from reading an already saved job.

`tests/integration/test_job_retrieval.py` verifies production POST-to-GET,
owning-team peer access, two-way team isolation, identical 404 responses, malformed
UUIDs and unauthorized callers without repository lookup, multi-team selection,
fresh status reads, safe database failure, and generated OpenAPI. A test-only
status update creates the running fixture; it is not a worker execution test.
US-009.2 extends these tests with all five response states, guarded persisted
outcomes, safe errors, UTC timestamps, invalid legacy rows, and actual ZAP count
retrieval. These cover the retrieval portions of FR-005–007, AC-002/AC-004 and
AC-017; full worker orchestration and cancellation execution remain separate.


## Guarded lifecycle persistence (US-008.1)

Future worker/service code calls `app/repositories/lifecycle.py` inside a
caller-owned transaction. `start_job` atomically matches pending state and no
cancellation request, records the execution owner, increments attempts, and
sets the start time. `finish_job` matches running state plus recorded worker and
writes completed/result or failed/error with a finish time in the same update.
`fail_pending_job` supports exhausted dispatch/recovery failure while still
pending. PostgreSQL evaluates each guard as part of the write; one competing
transition wins and terminal rows do not match later writes.

All functions return a Boolean changed-row result before caller commit. Database
exceptions propagate. The existing timestamp/identity trigger remains in force;
no schema change is required. There is no automatic retry, public execution
endpoint, running-job reclamation, or background worker integration in this story.
GET retrieval now exposes validated lifecycle data through US-009.2. Successful
and safe failure persistence are implemented in US-008.2/US-008.3. Renewable
claims exist in US-014.1; recovery and cancellation execution remain separate.
Application guards do not constrain privileged raw SQL or promise exactly-once
scanner execution. See [Guarded job lifecycle](job-lifecycle.md),
[Repository](design-pattern/repository.md), and
[Idempotent Consumer](design-pattern/idempotent-consumer.md).


## Versioned publication and recovery (US-006.1 and US-006.3)

`app/schemas/job_message.py` defines the shared version-one contract. The
`app/dispatchers/publication.py` operation claims committed intent in a short
transaction, builds validated parameters from the immutable saved job, publishes
to SNS outside the transaction, and conditionally records acknowledgment/failure
using the claim token. It never changes job status. A 60-second lease bounds
ownership; lost acknowledgments/final commits can still lead to duplicates.

`app/dispatchers/main.py` runs independently of HTTP requests using the same image.
Every two seconds it discovers up to ten due intents, ordered by scheduled time.
Discovery and atomic claiming share eligibility guards: fewer than five attempts,
no active reservation, no invalid-message error, pending uncancelled job, and
unpublished/unsuppressed due intent. Handled failures schedule jittered exponential
backoff; interrupted attempts recover after lease expiry plus saved backoff.
A restart preserves the budget. Five consumed attempts stop automatic publication
without deleting the job or intent; a final crashed attempt is observable from
its counter and expired lease even without a final log. Invalid payloads also
require operator action. It stops taking work on SIGTERM/SIGINT, closes resources,
and removes its liveness marker. See [Job publication](job-publication.md) for
inputs, safe logs, inspection SQL, duplicate risks and AC-007 verification.
The access-recovery test restarts an isolated dispatcher against LocalStack;
worker execution, real AWS and complete end-to-end recovery remain separate.
