# Repository

## Status and problem

US-002.2 implements the first job repository operation in
`app/repositories/jobs.py`. Creation needs to map validated submission data and
verified ownership consistently, without allowing request fields to become
model arguments that override ownership.

A repository puts application-specific persistence operations behind named
functions. This implementation uses one small function, not a generic base class
or a framework. Team-scoped reads and other operations remain later stories.

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

The repository returns a `Job` for the future acceptance service to associate
with dispatch intent. Atomic job/outbox orchestration is US-004.2; this function
alone does not implement it. No production submission endpoint exists yet.

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
workflow supplies that context to future routes. Direct initial SQL inserts do not
verify identity; application callers must use the trusted creation path.

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

This supports FR-027/FR-028 and AC-017's ownership foundation. Endpoint filtering,
shared team retrieval, actual worker/maintenance integration, and complete
cross-team regression remain later tasks. See
[Database storage and migrations](../database-migrations.md).
