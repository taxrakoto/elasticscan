# Safe failure persistence (US-008.3)

These reusable repository functions save safe failed outcomes to PostgreSQL.
They do not run a scanner, commit a transaction, change the deployed idle worker,
or automatically fail dispatch/recovery attempts. No migration or dependency
is needed. Public HTTP lifecycle serialization remains US-009.2.

## Error contract

`app/core/failure.py:safe_error(code)` returns a fresh dictionary containing only
`code` and its fixed `message`. Unknown codes raise `ValueError`.

| Code | Fixed message |
| --- | --- |
| `scanner_timeout` | Scanner execution exceeded its time limit. |
| `scanner_execution_failed` | Scanner execution failed. |
| `scanner_output_invalid` | Scanner output was missing or invalid. |
| `scanner_execution_invalid` | Scanner execution did not satisfy the baseline requirements. |
| `dispatch_exhausted` | Scan dispatch attempts were exhausted. |
| `recovery_exhausted` | Scan recovery attempts were exhausted. |

The first four codes belong to execution failures; the last two belong to
exhausted pending-job decisions. No raw exception, report, log, target query,
credential or stack trace becomes part of the stored public-safe error object.
Existing privileged SQL/ORM writes and lower-level helpers can bypass this
boundary; this is not global database enforcement or caller authentication.

## Execution failure

```python
from app.repositories.lifecycle import fail_job

# outcome comes from execute_zap_scan(adapter, request), outside a transaction.
if outcome.status == 'failed':
    with sessions.begin() as session:
        saved = fail_job(
            session,
            job_id=request.job_id,
            worker_id=worker_id,
            claim_token=request.claim_token,
            outcome=outcome,
        )
# Durable only after successful transaction exit.
```

`fail_job(session, *, job_id, worker_id, claim_token, outcome)` requires a UUID
claim token and a `ScanOutcome` with no counts and a recognized scanner failure
code. Invalid values raise `ValueError` before updating a job. It deliberately
ignores `outcome.error_message`, rebuilding the message from the catalog even if
a manually constructed outcome contains secrets.

The function delegates to `finish_job`: the job must be running, owned by the
worker/token, and have a strictly unexpired lease measured by database time after
locking the row. It atomically writes `failed`, the safe error, null result and
UTC finish time, and clears the active token/lease. Wrong owners, stale tokens,
expired claims, missing jobs and terminal rows return `False` without a write.
`True` means an update matched, not that it committed. Database errors propagate.

## Exhausted dispatch or recovery

```python
from app.repositories.lifecycle import fail_exhausted_job

# A trusted service has already determined that its retry bound is exhausted.
with sessions.begin() as session:
    saved = fail_exhausted_job(
        session, job_id=job_id, code='dispatch_exhausted',
    )
```

`fail_exhausted_job(session, *, job_id, code)` accepts only `dispatch_exhausted`
or `recovery_exhausted`. It delegates to `fail_pending_job`, writing failed state,
safe error, null result and finish time only while pending. It cannot replace a
running or terminal outcome. It does not count attempts, reclaim abandoned
running jobs, authenticate services, or implement a retry policy. Dispatch
exhaustion currently remains pending until a future service caller uses this
operation. Bounded running-job recovery remains US-014.3.

## Transactions, patterns and compatibility

Callers own short commit/rollback boundaries. Another connection sees the whole
previous state until commit. A later statement failure rolls back status, error,
finish time and claim changes together. An uncertain commit requires inspecting
durable state rather than assuming rollback. Read a fresh session or refresh
previously loaded ORM objects after bulk updates.

The existing `finish_job` and `fail_pending_job` remain trusted lower-level
compatibility operations with dictionary-only error validation. Intended normalized
failure callers should use the stricter functions. Direct SQL/ORM writes retain
existing shape constraints; arbitrary messages remain possible through those paths.

This reuses [Repository](design-pattern/repository.md) and consumes the existing
`ScanOutcome` [Value Object](design-pattern/value-object.md). Fixed-message
selection, validation, transactions and conditional SQL are techniques, not new
patterns. The benefit is one safe persistence boundary without duplicating claim
rules; the tradeoff is that internal callers must choose it and maintain the
catalog when adding a new scanner failure classification.

## Verification and acceptance boundaries

The additions to `tests/integration/test_result_summaries.py` use actual migrated
disposable PostgreSQL schemas. Controlled adapter exceptions prove interpretation
followed by durable safe failure; manually constructed outcomes prove message
reconstruction. Tests also cover null unused fields, UTC/identity preservation,
replacement database connections, team isolation, invalid inputs, stale claims,
duplicate/terminal protection, real statement-error rollback, pending exhaustion
versus claim, and completion versus failure competition.

See the [test guide](../tests/README.md#us-0083-safe-failure-persistence) for commands
and actual check results. Existing scanner tests separately cover cleanup; the new
tests launch no scanner. This implements the failure-persistence portions of
FR-009/FR-010/FR-011/FR-013/FR-025 and AC-004/AC-005, with public-safe stored errors
supporting NFR-011. Full AC-005 live-worker integration, HTTP error serialization,
queue acknowledgment, automatic lease renewal and bounded recovery remain pending.
