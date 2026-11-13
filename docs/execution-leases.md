# Execution claims and leases (US-014.1)

These reusable database operations give one execution attempt temporary authority
to process a saved job. The worker handler uses them before its adapter handoff;
US-014.2 now composes them with periodic renewal, SQS delivery and real scanner
execution in the worker source. Existing running containers are not replaced by
source changes. See [Worker delivery](worker-delivery.md).

## Fields and callable contract

All operations are in `app/repositories/lifecycle.py`. A claim token is a fresh
UUID identifying one execution attempt, not just a process name. `heartbeat_at`
records the latest accepted claim/renewal time; `lease_expires_at` bounds its
authority. Worker IDs must be nonempty, unpadded strings. Lease duration must be
a positive integer number of seconds representable by Python's `timedelta` and
the database date range. The default is a **provisional 60 seconds**, configurable
through `lease_seconds` on `claim_job`, `renew_job`, and `handle_message`.
US-014.2 adds validated `WORKER_LEASE_SECONDS`, `WORKER_HEARTBEAT_SECONDS` and
`WORKER_VISIBILITY_SECONDS` settings; the original callable defaults stay valid.

| Operation | Guard and return | Changes |
| --- | --- | --- |
| `claim_job(session, *, job_id, worker_id, lease_seconds=60, max_attempts=None)` | Pending, no cancellation request or existing claim metadata, below optional attempt limit; new UUID or `None` | Running, worker, fresh token, one additional attempt, start/heartbeat time, expiry |
| `renew_job(session, *, job_id, worker_id, claim_token, lease_seconds=60)` | Running, same worker and UUID token, strictly unexpired; Boolean | Heartbeat and expiry; never shortens an existing longer lease |
| `finish_job(session, *, job_id, worker_id, status, result=None, error=None, claim_token=None)` | Running, same worker; leased jobs also require same token and strictly unexpired lease; Boolean | Outcome/status/time atomically; clears token and expiry, retains worker and last heartbeat |

Expired claims cannot revive themselves or finish, and `claim_job` never takes
over running jobs. Unknown jobs, wrong owners, stale tokens and terminal states
cannot match the corresponding guards. Invalid worker IDs, durations, token types
or outcome shapes raise `ValueError`; database errors propagate.

Legacy `start_job` remains available for nonleased callers but requires absent
claim metadata. `finish_job` without a token accepts only nonleased running jobs;
omission cannot bypass a leased job's ownership. New worker handoffs use
`claim_job`, not the legacy start. These helpers are trusted internal operations;
privileged SQL/ORM can still bypass them. No database role restriction or lifecycle
trigger is added, and migration `0001` already supplies all storage fields.

## Transactions and database time

```python
from app.repositories.lifecycle import claim_job, renew_job, finish_job

with sessions.begin() as session:
    token = claim_job(session, job_id=job_id, worker_id=worker_id)
# Only a successful commit authorizes handoff. None means no claim.

if token is not None:
    # The US-014.2 execution wrapper calls this periodically before expiry.
    with sessions.begin() as session:
        renewed = renew_job(
            session, job_id=job_id, worker_id=worker_id, claim_token=token,
        )
    # A rejected or uncertain renewal must not be treated as retained authority.

    # This is a persistence example using fixture data, not real scanner output.
    with sessions.begin() as session:
        finished = finish_job(
            session, job_id=job_id, worker_id=worker_id, claim_token=token,
            status='completed', result={'high': 0},
        )
```

Callers own commit/rollback. A returned token or True becomes durable only after
commit. The handler commits before passing `ScanRequest(job_id, url, claim_token)`
to the adapter. The token is internal metadata, not an API/SQS field, target URL
component or scanner command. It lets later execution code correlate renewal and
terminal persistence to the same attempt.

Claim, renewal and token-protected completion lock the job with `SELECT ... FOR
UPDATE`, then read PostgreSQL `clock_timestamp()`, then issue a guarded update.
The time is obtained after waiting for the lock. Transaction-start `now()` would
be stale in a long transaction and could incorrectly accept an expired lease.
These operations serialize writes to one row; they do not hold a transaction
while scanning. Keep caller transactions short, commit promptly and use fresh
sessions. Leases are checked at the locked operation, not again at commit, and a
delayed commit consumes lease time. An uncertain commit must not trigger blind
execution or retry. SQLAlchemy's normal autoflush can flush unrelated session work;
bulk updates disable session synchronization, so refresh loaded ORM state or read
through a new session. All timestamps are timezone-aware; application connections
return UTC. The existing trigger maintains `updated_at`.

## Guarantees, tradeoffs and future work

The [Repository](design-pattern/repository.md) operations and
[Idempotent Consumer](design-pattern/idempotent-consumer.md) foundation protect
the current database claim and terminal outcome. UUID tokens distinguish attempts
even when a worker name is reused. Row locks, expiry and tokens are coordination
techniques, not extra design patterns. Locks add round trips and can wait on
competing transactions; callers must avoid long transactions and inconsistent
multi-row lock ordering.

The raw handoff still propagates adapter exceptions. The US-014.2 execution
wrapper now renews leases/visibility, interprets scanner output and commits guarded
outcomes before acknowledgment. `ScanInterrupted` leaves recoverable state;
the baseline adapter observes a stop event and kills its process group.

The US-014.3 conservative recovery repository fails expired non-cancel-requested
leased ZAP jobs as `recovery_exhausted`. It never reclaims or reruns them. The live
worker passes `max_attempts=1`; callers omitting that new optional argument retain
the original callable behavior. Row locks and fresh database time protect the
transition, and stale completion writes cannot match after token removal.

Physical exactly-once scanning is not promised. Cancel-requested jobs retain
requests for the future cancellation implementation; missing jobs cannot be
recreated from messages. Legacy unleased running jobs require operator action.
US-014.3 now schedules configurable recovery independently of active scans and
verifies hard process interruption after a committed claim and natural expiry.
[Execution budgets](execution-budgets.md) cover US-017.1. Current cancellation
metadata and absent-record guards are preserved; full cancellation/deletion APIs
remain later stories. Existing fields suffice; no migration is added.

## Verification

`tests/integration/test_execution_leases.py` uses eight migrated disposable-schema
PostgreSQL tests. It verifies simultaneous claims, committed token/UTC/ownership,
renewal ownership and nonshortening expiry, expired claim rejection, token omission
and stale-token rejection, competing terminal writes, rollback, current clock in
an older transaction, invalid inputs, unknown jobs and cancellation guards.
Replacement tokens and expiry changes are direct test fixtures, not implemented
recovery. Existing worker tests check the committed token reaches the adapter and
competing validated deliveries invoke it once; terminal-state fixtures remain
unchanged. No real scan, queue acknowledgment or deployed recovery is tested.

See the [test guide](../tests/README.md#us-0141-execution-claims-and-leases) for
commands and actual results. Tests remove their schemas and permanent application
jobs are untouched. Source-mounted tests do not deploy the implementation.


## Delivery integration verification

`tests/integration/test_worker_delivery.py` adds real PostgreSQL/LocalStack checks
for committed running state, repeated renewal, result/failure storage before
acknowledgment, duplicate terminal delivery, transaction failure/redelivery,
shutdown, renewal uncertainty, expired-claim failure, stale writers, preserved
cancellation requests, competing consumers and queue-loop execution. Actual ZAP
interruption/cleanup is verified separately in the isolated baseline fixture.
See [Worker delivery](worker-delivery.md) and the
[test guide](../tests/README.md#us-0142-delivery-and-us-0143-partial-recovery).

## Independent conservative recovery

`app/workers/recovery.py` commits at most the configured batch size per pass,
logs committed job IDs and retries storage failures after an interruptible
interval. The main worker owns thread startup/shutdown. A healthy available
worker resolves expired claims even while its consumer scans a different job.
One automatic execution attempt is the fixed cap; there is no automatic rescan.
A hard crash can leave physical scanner activity uncertain, so exactly-once
physical execution is not promised. Recovery fences old writers without
claiming external process termination. See [worker delivery](worker-delivery.md)
and [execution budgets](execution-budgets.md) for outage and backlog limits.
