# Guarded job lifecycle (US-008.1)

These reusable repository operations protect saved job transitions. The current
dispatcher and worker are idle bootstrap processes: no live queue delivery or
scanner execution is integrated here. Real execution acceptance under AC-004 and
AC-005 remains dependent on subsequent scanner/worker stories.

## Callable contract

All functions live in `app/repositories/lifecycle.py` and receive a SQLAlchemy
`Session` and job UUID. They return `True` if one row changed, or `False` if the
job is absent or does not match the transition guard. Database exceptions
propagate. The functions neither authenticate callers nor commit transactions.

| Function | Guard | Atomic changes |
| --- | --- | --- |
| `start_job(session, *, job_id, worker_id)` | Pending, with no recorded cancellation request | Running status, worker ID, incremented attempt count, start time |
| `finish_job(session, *, job_id, worker_id, status, result=None, error=None)` | Running, owned by the supplied worker | Completed/result or failed/error, unused outcome null, finish time |
| `fail_pending_job(session, *, job_id, error)` | Pending | Failed status, error object, result null, finish time |

Worker IDs must be nonempty strings without surrounding whitespace and must
identify the execution owner. `finish_job` accepts only completed with a result
dictionary and no error, or failed with an error dictionary and no result.
`fail_pending_job` requires an error dictionary and is reserved for trusted
service callers that have established exhausted dispatch/recovery policy.
These functions do not decide when attempts are exhausted.

The dictionary checks establish object shape only. Nonnegative severity counts,
scanner result normalization, stable error codes, safe messages, and removal of
secrets remain US-008.2/US-008.3. Do not pass arbitrary exception details as
production errors. Cancellation transitions and scanner termination remain US-011;
the cancellation-request start guard is not a completed cancellation feature.

## Transaction ownership

```python
from app.repositories.lifecycle import start_job

with sessions.begin() as session:
    changed = start_job(
        session,
        job_id=job_id,
        worker_id='worker-instance-123',
    )
# True becomes durable only after successful transaction exit/commit.
```

Future workers must not launch a scanner until the successful start transaction
commits. Terminal outcome persistence must also commit before queue acknowledgment.
Those caller integrations are not implemented here. A later failure in the same
transaction rolls back status, outcome, attempts, and timestamps together.
An exception during commit can leave the outcome uncertain; do not infer a
confirmed rollback from every connection error or blindly retry uncertain work.

The operations issue updates immediately without calling commit. SQLAlchemy's
usual autoflush can also flush other pending session work before the statement;
use a clean session unless intentionally composing one transaction. The existing
database consistency checks reject invalid state/outcome combinations.

## Atomic guards and timestamps

Each operation executes one `UPDATE ... WHERE ... RETURNING job_id` statement.
The state/worker checks and writes are one database operation. Concurrent updates
to the same job are coordinated by PostgreSQL; a competing state change makes
the losing guard stop matching. There is no application read-then-write gap.
The Boolean result deliberately does not explain which guard failed; a caller
needing that information must perform a separate read.

Starts use PostgreSQL `statement_timestamp()` for `started_at`; terminal writes
use it for `finished_at`. The existing `jobs_protect_identity` trigger maintains
`updated_at` and rejects changes to job identity, team, creator, scan type,
parameters, and creation time. Timestamps are `TIMESTAMPTZ`; application sessions
are configured to return UTC. No migration is needed and revision `0001` is
unchanged. Worker identity remains stored after completion for correlation.

Completed, failed, and cancelled rows match none of these functions. Duplicate
terminal writes and reopening attempts therefore preserve the full row,
including its original timestamps and outcome.

Bulk update synchronization is disabled (`synchronize_session=False`). Previously
loaded ORM objects may retain stale attributes in memory. Use a fresh session or
`session.refresh(job)` before reading updated values. Do not later flush stale
manual lifecycle edits that bypass the guarded repository.

## Guarantees and boundaries

This uses the [Repository](design-pattern/repository.md) pattern and provides
guarded persistence groundwork for an
[Idempotent Consumer](design-pattern/idempotent-consumer.md). Conditional SQL
updates are an atomic concurrency technique, not an additional design pattern.

The functions are trusted internal operations. There is no public execution-update
endpoint, worker authentication, or restricted deployed database role here.
Privileged raw SQL and direct ORM writes can bypass application transition
predicates; the existing trigger protects identity, not all lifecycle changes.

No running-job reclamation is implemented. Worker ID checks are sufficient for
this non-reclaiming foundation, but are not a per-attempt fencing token. Before
reclaiming jobs, US-014 must add claim tokens, renewable leases, stale-owner
rejection, and bounded recovery. No heartbeat/lease fields are changed here.
This does not promise exactly-once physical scanner execution across crashes.
Queue validation, visibility renewal, message acknowledgment, cancellation,
deletion coordination, and actual scanner execution remain subsequent work.

## Verification

From `elasticscan/`, with local PostgreSQL healthy:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_job_lifecycle -v
```

`tests/integration/test_job_lifecycle.py` applies actual migrations in a unique
schema for each test and drops only that schema afterward. Concurrent checks
use independent sessions in two threads, synchronized before issuing updates,
and reload persisted state through fresh sessions. They exercise competing
starts, wrong owners, competing terminal outcomes, pending failure versus start,
all terminal states, UTC/ownership preservation, invalid inputs, and rollback.
Outcome and cancelled-state fixtures are not real scanner results or implemented
cancellation behavior. Tests contact no target and create no permanent jobs.

These checks support FR-009/FR-010/FR-011/FR-013/FR-025's persistence foundation
and the transition portions of AC-004/AC-005. Complete AC-006/AC-008 lease,
recovery, consumer, and acknowledgment acceptance remains future work.


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
