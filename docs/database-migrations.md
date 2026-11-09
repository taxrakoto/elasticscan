# Database storage, ownership, and atomic acceptance (US-004.1, US-002.2, US-004.2)

SQLAlchemy models describe the desired tables in Python. Alembic revisions describe
how to change PostgreSQL to match them. Importing models or starting the API does
not create tables; migrations are an explicit operator action.

## Apply the initial schema

From `elasticscan/`, after local setup and PostgreSQL startup:

```sh
docker compose build api
docker compose run --rm --no-deps --entrypoint python api -m alembic upgrade head
docker compose run --rm --no-deps --entrypoint python api -m alembic current
docker compose run --rm --no-deps --entrypoint python api -m alembic check
```

Revision `0001` creates `jobs`, `dispatch_intents`, their constraints and indexes,
and the job immutability/update-timestamp trigger. Alembic records the applied
revision in `alembic_version`. Running `upgrade head` again preserves existing
records and applies only outstanding revisions. Credentials come from the Compose
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD` environment settings;
they are not written into migration files or `alembic.ini`.

## Stored contract

| Entity | Storage |
| --- | --- |
| Job identity | Application-generated UUID, immutable `team_id`, `created_by`, `scan_type`, JSONB parameters, and creation time |
| Lifecycle | `pending` default; constrained `running`, `completed`, `failed`, and reserved `cancelled` states |
| Outcomes | JSONB result for completion, JSONB error for failure; neither for pending/running/cancelled jobs |
| Timestamps | PostgreSQL `TIMESTAMPTZ`; application connections return UTC; database trigger refreshes `updated_at` on updates |
| Execution | Attempt count, worker identity, claim UUID, lease expiry, and heartbeat; job UUID supplies correlation |
| Cancellation reservation | Nullable requesting subject, request time, and completion time; behavior belongs to US-011.1 |
| Dispatch intent | One row per job, message schema version, publication/suppression times, attempts, next retry, claim/lease, and safe error code |

The dispatch row references the job rather than copying its immutable parameters
and scan type. A future publisher will build the versioned message and routing
attribute from that job. The stored schema version defaults to 1; this does not
yet establish a public message contract. Unpublished, unsuppressed intent has a
partial due-time index. Foreign keys reject orphan intent and cascade its cleanup
when a job is physically removed. Safe deletion and tombstones are later work.

Database checks enforce basic state/outcome consistency, required ownership,
JSON objects, nonnegative attempt counts, and paired claim/lease fields. The
trigger rejects changes to accepted identity and parameters even through raw SQL.
Team-context authorization is implemented in US-002.1 and ownership assignment
in US-002.2. US-009.1 implements team-scoped retrieval, and US-008.1 adds guarded
lifecycle repository operations using the existing schema. Scanner-specific result
validation, bounded recovery, and cancellation rules remain later stories.

## Verified immutable ownership

`app/repositories/jobs.py` provides `create_pending_job(session, *, submission,
context)`. It takes a validated `ZapSubmission` and a `TeamContext` from verified
authorization, explicitly copying only the context's `team_id` and `created_by`
into ownership columns. The request schema rejects both fields at the root and
inside `parameters`; it never supplies ownership. The repository does not accept
separate ownership overrides or arbitrary model keyword arguments.

The context is a trusted internal input, not a credential: type annotations and
the frozen dataclass cannot authenticate arbitrary Python callers. Future routes
must obtain context through `require_team_context`, not construct it from JSON.

`session.flush()` inserts the job and obtains its UUID/defaults without committing.
The caller controls commit/rollback. US-004.2's `accept_scan` service now supplies
that boundary and adds dispatch intent. The lower-level repository function alone
does not acknowledge accepted work or guarantee a matching outbox row.

Revision `0001` already defines required ownership and `jobs_protect_identity`.
No schema change or new migration is required. The trigger compares old and new
ownership and rejects transfer with SQLSTATE `23514`. It applies to ordinary ORM
updates, bulk updates, and raw SQL, so future worker and maintenance writes cannot
transfer ownership while the trigger is active. Execution metadata can change
without changing ownership.

This is defense in depth: request validation rejects ownership fields, the
repository maps verified context, and PostgreSQL prevents later transfer. A
database owner or superuser capable of disabling triggers is outside that
guarantee. Restricted service roles belong to deployment security work; these
tests exercise write mechanisms, not separate deployed worker/maintenance roles.
Database constraints alone also cannot verify that an arbitrary initial insert
came from an authenticated user; application creation must use the trusted path.

## Atomic acceptance

`app/db/session.py` provides explicit engine and session factories, without opening
connections at import time. `app/services/acceptance.py` provides
`accept_scan(sessions, *, submission, context)` and owns the complete acceptance
transaction. Supply a validated submission and verified authorized context:

```python
from app.db.session import make_engine, make_session_factory
from app.services.acceptance import accept_scan

engine = make_engine()
sessions = make_session_factory(engine)
try:
    # submission is validated; context comes from verified authorization.
    accepted = accept_scan(
        sessions,
        submission=submission,
        context=context,
    )
    # Both records have committed before accepted becomes available here.
finally:
    engine.dispose()
```

The service opens a session/transaction, inserts the job using the existing
ownership repository, adds its dispatch intent, flushes both, and lets the
transaction context commit. It returns `AcceptedScan(job_id, scan_type, pending)`
only after successful exit. This frozen value object is a snapshot of acceptance,
not live job status, and does not depend on an open SQLAlchemy session.

Flush sends inserts to PostgreSQL inside the transaction; commit makes the pair
durable and visible to other sessions. Insert errors and failures before commit
roll back both. Exceptions propagate; no acceptance result is returned on error.
The context closes the session on success or failure. The service does not join
an existing caller transaction and should be called with the session factory.

A connection loss during commit may leave its outcome unknown to the caller:
PostgreSQL still commits both records or neither, but an exception does not prove
that a confirmed rollback occurred. This service does not blindly retry uncertain
commits or provide request deduplication. Future HTTP outage handling must
preserve that distinction.

HTTP acknowledgment is implemented in US-003.2 and team-scoped status retrieval
in US-009.1. Publication/retry behavior remains US-006. No network publication or
scanner execution is implemented by the persistence layer. The schema
alone does not force arbitrary direct inserts to include intent; application
acceptance must use this service.

See [Transactional Outbox](design-pattern/transactional-outbox.md),
[Repository](design-pattern/repository.md), and
[Value Object](design-pattern/value-object.md) pattern documents for this handoff.

## Change the schema later

1. Change the SQLAlchemy model.
2. Against an up-to-date development database, generate a **new** revision:

   ```sh
   docker compose run --rm --no-deps --user 0:0 -v "$PWD:/workspace" -w /workspace --entrypoint python api -m alembic revision --autogenerate -m "Describe the schema change"
   ```

3. Inspect the generated `upgrade()` and `downgrade()`. Add data backfills and
   hand-written trigger/function changes where needed. Autogeneration does not
   detect every database change and cannot decide how existing data should move.
4. Test on disposable PostgreSQL data, including an upgrade from the previous
   revision. Rebuild the image, apply `upgrade head`, and run `alembic check`.
5. Commit the model, migration, and tests together. Apply migrations in staging
   before production, with application compatibility and deployment order reviewed.

Do not edit a revision already applied to a shared database. Additive changes can
usually precede application deployment. Required fields may need a nullable phase
and a backfill; renames/removals may need several compatible deployments.

`downgrade base` for revision `0001` **drops both tables and their data**. Use it
only on disposable databases. Production recovery requires an explicit data
recovery plan. The integration suite exercises downgrade only in its own schema.

## Verification

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest discover -s tests -t . -p 'test_*.py' -v
```

Each PostgreSQL test creates a unique schema, applies actual Alembic migrations,
and removes that schema afterwards. The local test role must be allowed to create
schemas. No existing application tables or records are reset. Without
`RUN_POSTGRES_TESTS=1`, the database tests are explicitly skipped; the submission
suite can still run offline.

US-002.2 adds signed-token-to-PostgreSQL ownership tests for two teams, another
member of the same team, and explicit multi-team selection. It tests transfer
rejection through object, bulk, and raw-SQL updates, confirms original ownership
after each rejection, and verifies permitted metadata updates preserve ownership.

US-004.2 adds five acceptance tests: successful paired commit/reload, rollback
after the job insert, rollback after a real dispatch constraint violation,
rollback after both inserts just before commit, and external-session visibility
only after commit. Failure injection is confined to tests. The visibility check
uses a separate connection; it does not infer persistence from the ORM cache.

References: [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
and [SQLAlchemy PostgreSQL documentation](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).


## Guarded transitions without a schema change (US-008.1)

`app/repositories/lifecycle.py` reuses existing status, worker ID, attempt count,
outcome, and timestamp columns. Revision `0001` is unchanged. Its consistency
checks ensure terminal timestamps and the appropriate result/error object, and
its trigger preserves immutable identity and maintains `updated_at`. The new
functions add conditional application update predicates; they do not introduce
a database-wide lifecycle trigger or restrict privileged raw SQL.

The caller owns the transaction and must commit before treating a True result
as durable. Failure rolls back the whole transition. Timestamps use PostgreSQL
`statement_timestamp()` and UTC-configured connections. The seven checks in
`tests/integration/test_job_lifecycle.py` run on migrated disposable schemas,
including real independent concurrent sessions and fresh-session reloads.
See [Guarded job lifecycle](job-lifecycle.md) for interfaces and operational limits.
