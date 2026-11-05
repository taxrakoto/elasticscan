# Database storage and migrations (US-004.1)

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
Authorization, scanner-specific parameter/result validation, guarded state
transitions, bounded recovery, and cancellation rules remain later stories.

## Use transactions

`app/db/session.py` provides explicit engine and session factories, without opening
connections at import time. A caller owns the transaction boundary:

```python
from app.db.session import make_engine, make_session_factory
from app.models import Job, DispatchIntent

engine = make_engine()
Session = make_session_factory(engine)
try:
    with Session.begin() as session:
        job = Job(
            team_id=verified_team_id,
            created_by=verified_subject,
            scan_type=submission.scan_type,
            parameters=submission.parameters.model_dump(mode='json'),
        )
        session.add(job)
        session.flush()  # Assign the UUID and insert the referenced job first.
        session.add(DispatchIntent(job_id=job.job_id))
finally:
    engine.dispose()
```

This illustrates the storage primitives. The production acceptance workflow,
rollback tests, HTTP acknowledgment, and verified identity integration belong to
US-004.2, US-003.2, and US-002.2. No submission route or publisher is added here.
The schema alone does not force every job insertion to include an intent.

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

References: [Alembic tutorial](https://alembic.sqlalchemy.org/en/latest/tutorial.html)
and [SQLAlchemy PostgreSQL documentation](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html).
