# Repository

## Status and problem

US-002.2 implements the first job repository operation in
`app/repositories/jobs.py`. Creation needs to map validated submission data and
verified ownership consistently, without allowing request fields to become
model arguments that override ownership.

A repository puts application-specific persistence operations behind named
functions. This implementation uses focused functions, not a generic base class
or a framework. US-009.1 adds a focused team-scoped read operation;
listing and other operations remain later stories.

## Components and data flow

1. Authentication verifies the JWT; team authorization resolves `TeamContext`.
2. `ZapSubmission` validates scan input and rejects unknown root/nested fields,
   including `team_id` and `created_by`.
3. `create_pending_job` receives the submission, context, and SQLAlchemy session.
   It explicitly assigns ownership from the context and parameters from the
   validated submission. It accepts no ownership overrides or arbitrary fields.
4. It adds and flushes the job. PostgreSQL assigns defaults and the UUID becomes
   available. Flush is not commit: the caller owns the transaction and its errors.
5. PostgreSQL's existing trigger rejects later ownership changes through ordinary
   object updates, bulk updates, and direct SQL. Allowed metadata updates preserve
   ownership.

The repository returns a `Job` to US-004.2's `accept_scan` service, which associates
dispatch intent and commits both in one transaction. The repository does not
commit; the service supplies that boundary and returns an immutable acceptance
snapshot only after commit. US-003.2 exposes that operation through production
submission.

US-009.1 adds `find_team_job(session, job_id, context)`. It selects a `Job` with
both the UUID and verified `context.team_id` in the same SQL query, returning
`None` when no accessible record exists. It does not load an unfiltered job first
or limit access to the creator. `GET /scan/{job_id}` owns the read session and
serializes the returned object before closing it. Unknown and cross-team jobs
therefore share the same HTTP 404 path. The repository itself has no HTTP logic.

## Benefits, tradeoffs, and guarantees

An explicit creation path avoids repeating ownership mapping in future handlers.
The session is supplied by the caller, so this operation can join a larger
transaction without committing early. Separating persistence from HTTP also lets
the database behavior be tested without adding a temporary production endpoint.

The extra function is another interface to maintain. It remains coupled to the
SQLAlchemy session and model; it does not claim database independence.

The repository trusts its internal inputs. `TeamContext` is a
[Value Object](value-object.md), not cryptographic proof: callers must obtain it
from verified authorization. The existing [Dependency Injection](dependency-injection.md)
workflow supplies that context to production submission and retrieval routes.
Direct initial SQL inserts do not verify identity; application callers must use
the trusted creation path.

Request rejection, explicit mapping, and database immutability are defense in
depth, a security principle. The trigger is the database enforcement mechanism.
A privileged database owner who can disable it is outside the guarantee;
restricted deployed service roles remain separate security work.

## Verification and requirements

`tests/integration/test_job_storage.py` verifies signed identity → authorized
context → repository → committed PostgreSQL ownership, including two teams, two
members of one team, and multi-team selection. It also rejects both ownership
field changes through ORM, bulk, and raw-SQL paths and checks permitted metadata
updates. Each test applies the real migration to its own disposable schema.

Submission fixtures in `tests/fixtures/contracts/v1/scan-submission.json` exercise
ownership rejection at both request levels through schema and HTTP tests.
US-004.2 extends storage tests to cover the repository as part of atomic
acceptance, including paired commit, rollback failures, and transaction visibility.

This supports FR-027/FR-028 and AC-017's ownership foundation. US-009.1 adds
endpoint filtering and shared team status retrieval; actual worker/maintenance
integration and complete cross-team regression remain later tasks. See
[Database storage and migrations](../database-migrations.md).


`tests/integration/test_job_retrieval.py` verifies production submission followed
by owning-team and peer retrieval, reciprocal isolation for two teams, identical
unknown/cross-team 404s, validated multi-team selection, current persisted status,
and safe read failure. These support FR-006/FR-027/FR-028 and the read portion of
AC-017. A test fixture writes a running status directly; that does not establish
a worker transition. Lifecycle serialization remains US-009.2.

Centralizing the query reduces the risk of a route omitting its team predicate,
but future handlers must still call this restricted operation with verified
context. This remains an application-level control rather than PostgreSQL
row-level security. The function is coupled to SQLAlchemy and returns an ORM
object whose session lifetime the caller must manage. The route explicitly
limits public response fields; the repository is not a general serializer.


## Guarded lifecycle writes (US-008.1)

`app/repositories/lifecycle.py` names three internal persistence operations:
`start_job`, `finish_job`, and `fail_pending_job`. Future worker/service callers
supply a session, job UUID, and trusted execution inputs. Each function executes
one conditional PostgreSQL update and returns whether a row changed. The caller
owns commit/rollback, so these functions can compose with a larger transaction.
No HTTP execution-update endpoint is exposed.

`start_job` requires pending state and no recorded cancellation request; it
records a worker, increments attempts once, and writes the start time.
`finish_job` requires running state and the recorded worker, writing one outcome,
terminal status, and finish time together. `fail_pending_job` permits an internal
exhausted-service failure only before running begins. Terminal rows never match
any of these updates. Database errors propagate without being converted into a
successful transition. UTC times come from PostgreSQL; the existing trigger
maintains `updated_at` and protects immutable identity and parameters.

Conditional updates are a concurrency technique supporting the Repository and
future Idempotent Consumer patterns, not a new design pattern. Centralized
predicates avoid inconsistent read-then-write checks, but every future execution
caller must use them. These are trusted internal operations, not authentication
or database role enforcement. Direct SQL/ORM lifecycle writes remain possible.
The True/False interface does not distinguish missing jobs from wrong state or
wrong ownership; a separate read is needed if the caller requires a reason.

SQLAlchemy session synchronization is disabled for these bulk updates. Read
through a fresh session or explicitly refresh a previously loaded object.
`tests/integration/test_job_lifecycle.py` verifies competing sessions, terminal
immutability through the helpers, ownership/UTC preservation, and rollback.
Semantic result/error validation, bounded recovery, and cancellation
remain subsequent stories. See [Guarded job lifecycle](../job-lifecycle.md) and
[Idempotent Consumer](idempotent-consumer.md).

## Execution leases (US-014.1)

`claim_job` and `renew_job` extend the same focused repository module. A claim
returns a fresh UUID token or None. Renewal and token-protected `finish_job` require
the current worker/token pair and a strictly unexpired lease. These operations lock
the job row before reading database clock time and performing guarded updates;
callers still own commit/rollback. Lease updates preserve team/creator/parameters,
and successful completion clears active claim metadata atomically with the outcome.
Legacy nonleased operations remain available but cannot finish a leased job by
omitting its token. The validated worker handoff now uses leased claims.

This adds serialization and database round trips to prevent stale ownership
decisions; transactions must remain short. It provides guarded persistence, not
worker authentication, global SQL enforcement or physical scanner termination.
Eight disposable-schema integration tests verify competition, renewal, expiry,
rollback, token fencing and current-clock checks. See
[Execution claims and leases](../execution-leases.md) for examples, configuration,
the provisional default, stale ORM-state cautions and remaining recovery work.

## Validated successful completion (US-008.2)

`app/repositories/lifecycle.py:complete_job` consumes the shared `SeverityCounts`
value object, checks nonnegative integers and a UUID token, then delegates to
`finish_job`. This centralizes the intended success-write boundary without
duplicating lease, owner or timestamp rules. It atomically writes the result and
completed state while the caller retains transaction ownership. Validation is
an ordinary boundary technique; no new design pattern is introduced.

The benefit is a tool-independent normalized persistence contract. The tradeoff
is that trusted callers must choose this stricter operation: legacy `finish_job`
and direct SQL retain their earlier validation. No global SQL severity constraint
or live worker orchestration is added. Disposable-schema tests verify rollback,
visibility, replacement application connections, team-filtered retrieval, expiry
and competing writers; a two-stage helper also verifies actual isolated ZAP
counts. See [Atomic result summaries](../result-summaries.md).

## Safe failure persistence (US-008.3)

`app/repositories/lifecycle.py:fail_job` consumes the existing `ScanOutcome` value
object, validates a scanner failure and UUID token, then delegates to the guarded
`finish_job` update. `fail_exhausted_job` accepts only exhausted dispatch/recovery
codes and delegates to `fail_pending_job`. `app/core/failure.py:safe_error` rebuilds
a code/message object from a fixed catalog; supplied outcome messages are ignored.
The caller supplies a short transaction and owns commit/rollback.

Centralizing the safe boundary prevents raw exception/report data from becoming
stored public-safe errors while reusing existing atomic owner/state/lease checks.
The tradeoff is that callers must choose these functions: legacy dictionary-based
helpers and privileged SQL/ORM paths retain their previous semantics. Pending
exhaustion decisions remain trusted service inputs; no retry-policy enforcement,
worker orchestration or HTTP serialization is introduced. Fixed-message mapping
and input validation are techniques, not additional design patterns.

The additions to `tests/integration/test_result_summaries.py` verify classified
exceptions, message reconstruction, fresh-connection/team retrieval, null fields,
UTC times, rollback, stale/duplicate protection and competing outcomes. See
[Safe failures](../safe-failures.md) for interactions, examples and acceptance limits.
