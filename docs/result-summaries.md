# Atomic result summaries (US-008.2)

`app/repositories/lifecycle.py:complete_job` is a reusable persistence entry point
for successful normalized scans. It saves the high/medium/low summary, completed
status, finish timestamp and null error together, clearing the active claim token
and lease expiry. It does not run a scanner, commit a transaction, poll a queue or
change the deployed idle worker. There is no database migration or new dependency.

## Calling the completion function

```python
from app.repositories.lifecycle import complete_job

# outcome comes from execute_zap_scan(adapter, request).
if outcome.status == 'completed':
    with sessions.begin() as session:
        saved = complete_job(
            session,
            job_id=request.job_id,
            worker_id=worker_id,
            claim_token=request.claim_token,
            counts=outcome.counts,
        )
# Completion is durable only after successful transaction exit.
```

Use trusted internal execution metadata and a normalized `SeverityCounts` object.
The function requires a UUID token and all count values must be integers greater
than or equal to zero. Booleans, strings, fractions, negative values and missing
count objects raise `ValueError` before an update. Zero findings is valid. The
shared dataclass is immutable but allows direct construction with invalid values;
this persistence boundary therefore validates the values again.

`complete_job` delegates to the existing guarded `finish_job` update. The job must
be running, belong to the supplied worker and token, and have a strictly unexpired
lease measured by current PostgreSQL time after locking the row. A stale writer,
duplicate, unknown job, pending job or wrong owner returns `False` without an
update. `True` means the update matched, not that the caller has committed.
Database errors propagate. Previously loaded ORM objects require refresh or a
new session because lifecycle updates disable session synchronization.

## Atomicity and durability

The caller owns a short transaction around persistence; no database transaction
should remain open during scanner execution. A separate reader sees the whole
previous state until commit. If a subsequent statement fails and the transaction
rolls back, status, counts, error, timestamps and claim metadata all retain their
previous values. A connection failure during commit can leave the outcome
uncertain; inspect durable state rather than assuming rollback or repeating a scan.

After commit, a new engine, pool and session factory retrieve the same result using
the existing team-scoped repository query. PostgreSQL is the source of truth;
neither a raw report nor scanner process memory is needed for retrieval. The
tests simulate replacement of application database resources, not a PostgreSQL
restart or power-loss durability experiment.

The stricter function is the intended entry point for normalized successful
scans. The older `finish_job` function retains its dictionary-only validation for
compatibility. Direct SQL and ORM writes also retain existing database shape
constraints; this story does not enforce severity semantics globally in SQL.
It does not authenticate internal callers or introduce restricted database roles.

## Verification and remaining integration

`tests/integration/test_result_summaries.py` uses migrated disposable schemas and
real PostgreSQL. It covers normalized ZAP exit 0/1/2 fixtures, zero/nonzero counts,
validation, UTC/identity preservation, application resource replacement, team
filtering, uncommitted visibility, a real database statement failure, ownership,
claim expiry, duplicates and simultaneous competing completions.

```sh
docker compose build zap-worker
python3 -B scripts/verify_result_summaries.py
```

The helper first runs actual ZAP in the existing isolated container-local fixture.
The successful integration test emits only `ZAP_RESULT_SUMMARY=` and three integer
counts. After both scanner tests pass, the helper validates this summary and passes
it as an environment argument to the PostgreSQL tests in a separate API test
container. The actual-summary test commits and retrieves those counts through a
new pool/session factory. No report body, target content or credentials cross
this handoff. The scanner container still has no external network or database
access; the helper does not add a gateway or production policy exception.

See the [test guide](../tests/README.md#us-0082-atomic-result-summaries) for focused
and regression commands and actual results. The worker image continues to copy
the existing lifecycle module explicitly; imports are checked without a source
mount so test mounts cannot conceal missing runtime files.

This implements the successful-result persistence portion of FR-009/FR-011/
FR-013/FR-025 and AC-004. Safe failure persistence and the AC-005 path remain
US-008.3; lifecycle-specific HTTP result serialization remains US-009.2. Live queue
consumption, periodic lease renewal and full network enforcement remain separate.
The reusable worker handler still discards raw adapter output; this story provides
the completion function for its later orchestration. Reused patterns are
[Repository](design-pattern/repository.md) and the existing count
[Value Object](design-pattern/value-object.md); transactions and conditional
updates are techniques, not new patterns.
