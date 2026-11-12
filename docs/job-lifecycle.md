# Guarded job lifecycle (US-008.1)

These reusable repository operations protect saved job transitions. The dispatcher
publishes versioned messages with bounded recovery; the deployed worker remains
an idle bootstrap process. No scanner execution is integrated here.
Real execution acceptance under AC-004 and
AC-005 remains dependent on subsequent scanner/worker stories.

## Callable contract

All functions live in `app/repositories/lifecycle.py` and receive a SQLAlchemy
`Session` and job UUID. Boolean operations return `True` if one row changed, or
`False` if the job is absent or does not match the transition guard. US-014.1's
`claim_job` instead returns a new UUID token or `None`. Database exceptions
propagate. The functions neither authenticate callers nor commit transactions.

| Function | Guard | Atomic changes |
| --- | --- | --- |
| `start_job(session, *, job_id, worker_id)` | Pending, no cancellation request or claim metadata; legacy nonleased path | Running status, worker ID, incremented attempt count, start time |
| `claim_job(session, *, job_id, worker_id, lease_seconds=60)` | Pending, no cancellation request or claim metadata | Running, worker/token, incremented attempt, start/heartbeat/expiry |
| `renew_job(session, *, job_id, worker_id, claim_token, lease_seconds=60)` | Running, same worker/token and strictly unexpired | Heartbeat and nonshortening expiry |
| `finish_job(session, *, job_id, worker_id, status, result=None, error=None, claim_token=None)` | Running, owned by supplied worker; leased jobs require current unexpired token | Completed/result or failed/error, unused outcome null, finish time, clears active token/expiry |
| `complete_job(session, *, job_id, worker_id, claim_token, counts)` | Running, current worker/token and strictly unexpired lease; validated `SeverityCounts` | Completed/result, error null, finish time, clears active token/expiry |
| `fail_pending_job(session, *, job_id, error)` | Pending | Failed status, error object, result null, finish time |

Worker IDs must be nonempty strings without surrounding whitespace and must
identify the execution owner. `finish_job` accepts only completed with a result
dictionary and no error, or failed with an error dictionary and no result.
`fail_pending_job` requires an error dictionary and is reserved for trusted
service callers that have established exhausted dispatch/recovery policy.
These functions do not decide when attempts are exhausted.

The legacy dictionary checks establish object shape only. US-008.2 adds
`complete_job` for strict nonnegative integer counts from `SeverityCounts`,
including required claim ownership. It reuses the same atomic update and caller
transaction. US-007.3 supplies ZAP normalization; US-008.3 adds
[safe failure persistence](safe-failures.md). See also
[Atomic result summaries](result-summaries.md). Do not pass
arbitrary exception details as production errors. Cancellation transitions and
scanner termination remain US-011;
the cancellation-request start guard is not a completed cancellation feature.

## Transaction ownership

```python
from app.repositories.lifecycle import claim_job

with sessions.begin() as session:
    token = claim_job(
        session,
        job_id=job_id,
        worker_id='worker-instance-123',
    )
# Token becomes durable only after successful transaction exit/commit.
```

Workers must not launch a scanner until the successful start transaction
commits. US-007.1's reusable `app/workers/zap.py:handle_message` validates the
message against the saved job and, since US-014.1, commits a leased claim before
calling its injected adapter. Its tests use a recording adapter; real ZAP
execution is pending.
Terminal outcome persistence must also commit before queue acknowledgment;
those integrations are not implemented here. A later failure in the same
transaction rolls back status, outcome, attempts, and timestamps together.
An exception during commit can leave the outcome uncertain; do not infer a
confirmed rollback from every connection error or blindly retry uncertain work.

The operations issue updates immediately without calling commit. SQLAlchemy's
usual autoflush can also flush other pending session work before the statement;
use a clean session unless intentionally composing one transaction. The existing
database consistency checks reject invalid state/outcome combinations.

## Atomic guards and timestamps

The original nonleased operations use one conditional update. US-014.1's claim,
renewal and token-protected completion first lock the row and obtain current
database time, then execute a guarded update. The lock is held until the caller's
transaction ends. Concurrent updates to the same job are coordinated by
PostgreSQL; a competing state change makes
the losing guard stop matching. There is no application read-then-write gap.
The Boolean/optional-token result deliberately does not explain which guard
failed; a caller needing that information must perform a separate read.

Legacy starts use PostgreSQL `statement_timestamp()` for `started_at`; leased
claims and terminal writes use current `clock_timestamp()` (after row locking
for leased operations). Pending service failures retain `statement_timestamp()`.
The existing `jobs_protect_identity` trigger maintains
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

US-014.1 implements per-attempt claim tokens, renewable leases and stale-owner
rejection. New worker handoffs use these operations; nonleased compatibility
helpers cannot finish leased jobs without a token. See
[Execution claims and leases](execution-leases.md) for duration configuration,
transaction timing, examples and limits. There is no periodic renewal loop or
running-job reclamation; bounded recovery remains US-014.3.
This does not promise exactly-once physical scanner execution across crashes.
Queue polling, visibility renewal, message acknowledgment, cancellation,
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
and the transition portions of AC-004/AC-005. US-014.1 adds concurrent leased
claims, renewal and stale-writer tests. Complete AC-008 recovery, consumer, and
acknowledgment acceptance remains future work.


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

## Safe failure entry points (US-008.3)

`fail_job(session, *, job_id, worker_id, claim_token, outcome)` validates a failed
`ScanOutcome` and UUID token, rebuilds its error from the fixed catalog, and calls
`finish_job`. It retains running-state, worker/token and unexpired-lease guards.
`fail_exhausted_job(session, *, job_id, code)` accepts only exhausted dispatch or
recovery codes and delegates to the pending-only transition. Both return whether
an update matched; the caller still owns commit/rollback. Invalid inputs raise
`ValueError`; database failures propagate.

These are intended safe persistence boundaries. The earlier dictionary-based
helpers retain compatibility and do not promise safe message semantics. Neither
new function wires a live worker or determines retry exhaustion. See
[Safe failures](safe-failures.md) for examples, exact messages, verification and
remaining integration limits.
