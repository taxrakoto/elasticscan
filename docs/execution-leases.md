# Execution claims and leases (US-014.1)

These reusable database operations give one execution attempt temporary authority
to process a saved job. The worker handler uses them before its adapter handoff;
the deployed worker remains idle. There is no periodic renewal loop, SQS consumer,
or real scanner integration yet.

## Fields and callable contract

All operations are in `app/repositories/lifecycle.py`. A claim token is a fresh
UUID identifying one execution attempt, not just a process name. `heartbeat_at`
records the latest accepted claim/renewal time; `lease_expires_at` bounds its
authority. Worker IDs must be nonempty, unpadded strings. Lease duration must be
a positive integer number of seconds representable by Python's `timedelta` and
the database date range. The default is a **provisional 60 seconds**, configurable
through `lease_seconds` on `claim_job`, `renew_job`, and `handle_message`.
No environment setting or service timer is introduced in this story.

| Operation | Guard and return | Changes |
| --- | --- | --- |
| `claim_job(session, *, job_id, worker_id, lease_seconds=60)` | Pending, no cancellation request or existing claim metadata; new UUID or `None` | Running, worker, fresh token, one additional attempt, start/heartbeat time, expiry |
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
    # A future execution loop must call this periodically before expiry.
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

An adapter failure after commit leaves running state with an expiring claim;
there is no automatic retry, reclamation, completion or scanner termination here.
The future execution loop must renew before expiry and stop scanner resources
when ownership is lost. A database token cannot prevent an old process making
network requests. Exactly-once physical scanning across crashes is not promised.
Pending cancellation requests block claims; running cancellation, deletion
tombstones and recovery coordination remain their respective stories.

This implements the concurrent-claim and late-writer protections in FR-020 and
AC-006, with groundwork for FR-012/FR-019/FR-021/NFR-013/NFR-014 and AC-008.
SQS visibility renewal and deletion after durable outcomes remain US-014.2;
bounded abandoned-job detection/recovery and graceful shutdown remain US-014.3.
Full AC-008 cannot pass until those integrations exist. Semantic scanner outcomes
and safe public error formatting remain US-008.2/US-008.3.

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
