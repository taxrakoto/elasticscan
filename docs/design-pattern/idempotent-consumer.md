# Idempotent Consumer

**Status:** US-008.1 implements guarded lifecycle persistence as groundwork.
US-007.1 adds reusable message validation and a committed adapter handoff.
US-014.1 adds per-attempt claim tokens, renewable execution leases and guarded
terminal writes requiring a current unexpired claim.
US-014.2 implements queue consumption, renewal, scanner/outcome composition and
acknowledgment in source. The partial US-014.3 phase adds conservative expired-claim
failure. Existing running workers keep their old image until recreated; full
cancellation/deletion/recovery acceptance remains pending.

## Problem and application

The [transactional outbox](transactional-outbox.md) can publish a message more than
once when an acknowledgment is lost or publication completion cannot be saved.
SQS can also redeliver messages. A worker must therefore treat repeated delivery
of a job as a normal recovery case.

An idempotent consumer makes repeated processing preserve the intended business
outcome. For ElasticScan, the stable job ID connects messages to PostgreSQL job
state. An SQS receipt handle identifies a particular delivery and is not a stable
job identity.

## Worker delivery contract

1. Validate the message version, scan type, and parameters against the stored job.
   Malformed or inconsistent messages must not start the scanner.
2. Inspect the job and atomically acquire an execution claim under the lifecycle
   rules. Database coordination must prevent multiple valid concurrent claims.
3. If the job already has a terminal outcome, preserve it and acknowledge the
   message according to the terminal-state rules. A live competing claim must
   not launch another scan.
4. Execute the scanner under the current claim, renewing execution leases and SQS
   visibility as required. Persist outcomes with conditional writes that reject
   a stale claim owner.
5. Delete the SQS message only after the outcome is durably saved or an existing
   terminal outcome has been confirmed. A storage failure leaves delivery
   available for recovery.

Deletion tombstones and cancellation state must also suppress delayed execution
under their respective lifecycle rules. Missing jobs and malformed messages need
an explicit recovery or dead-letter policy; they must not be treated as permission
to reconstruct a job from untrusted message data.

## Guarantees and tradeoffs

This protects persisted outcomes and valid execution claims. It does not provide
exactly-once physical scanner execution across crashes: an old scanner process
could survive after its worker loses a lease. Recovery must coordinate scanner
termination and replacement execution as well as rejecting stale database writes.

Persisted claims, lease renewal, guarded updates, and reconciliation add complexity.
Process-local locks or a permanent `processed` flag written before execution are
insufficient: locks disappear on restart, while an early flag can strand a job
whose worker crashed before storing an outcome.

## Components and verification

The implementation spans `app/workers/`, `app/repositories/`,
`app/messaging/`, and `app/scanners/`. These are responsibility boundaries,
not evidence of completed feature logic.

Verify competing deliveries, redelivery after completion, expired-claim recovery,
stale-owner writes, terminal persistence failure, cancellation, and delayed delivery
after deletion. Trace these checks to FR-020/FR-021 and AC-006/AC-008, with the
applicable cancellation/deletion criteria in the [requirements](../requirements.md).


## Implemented lifecycle foundation (US-008.1)

`app/repositories/lifecycle.py` provides `start_job`, `finish_job`, and
`fail_pending_job`. Each uses one conditional update with `RETURNING`: pending
status permits one start, running plus recorded worker identity permits one
terminal outcome, and pending status permits one exhausted-service failure.
Completed, failed, and cancelled rows cannot match these writes. The caller
owns the session/transaction; a returned True becomes durable only after commit.
Outcome data, status, and database-clock UTC timestamps change atomically.

Repeated terminal writes preserve the full stored row, including timestamps.
`tests/integration/test_job_lifecycle.py` checks simultaneous starts, competing
completion/failure writes, start versus pending failure, wrong-worker writes,
all terminal states, UTC ownership preservation, and transaction rollback using
independent PostgreSQL sessions. Outcome and cancelled rows are test fixtures,
not evidence of scanner output validation or cancellation execution.

These guards are application repository controls, not a database-wide transition
trigger or proof of exactly-once physical execution. Privileged raw SQL/ORM
writes can bypass transition predicates. Worker IDs identify the recorded
execution owner. US-014.1 now adds per-execution tokens and renewable leases;
the partial US-014.3 phase fails expired claims without reclaiming them.
US-014.2 now supplies receipt handling and terminal confirmation before
acknowledgment. US-008.2/US-008.3 supply strict results and safe failures. See
[Guarded job lifecycle](../job-lifecycle.md) for the callable contract and limits.

## Validated handoff (US-007.1)

`app/workers/zap.py:handle_message` validates the shared message version/schema,
checks it against the saved job, then commits a start before invoking the
injected scanner adapter. Concurrent deliveries and redelivery cannot invoke the
adapter twice through this non-reclaiming handler. Missing or inconsistent jobs
are preserved without invocation; pending cancellation requests block starts.
Tests confirm terminal-state preservation and precommit failure exclusion.

The raw handoff remains a foundation without direct receipt handling. US-014.2
composes it with polling, acknowledgment, terminal persistence, renewal and ZAP.
Its raw adapter exceptions still propagate; the production execution wrapper and
partial recovery repository provide the additional behavior described below. See [Adapter](adapter.md) for decisions,
component interactions, tests and remaining acceptance boundaries.

## Leased claims (US-014.1)

The handler now uses `claim_job`, forwarding its committed token through the
immutable scanner request. `renew_job` and `finish_job` require the same worker,
token and an unexpired lease for running leased jobs. Token omission cannot bypass
the guard. Row locks serialize decisions; current database time is obtained after
locking. Terminal persistence clears active token/expiry while preserving outcome
and worker correlation. Expired claims cannot renew, finish or reclaim themselves.

Eight PostgreSQL tests verify claim/terminal competition, expiry, renewal,
stale-token rejection, rollback and time checks; worker tests verify committed
token handoff and duplicate-delivery exclusion. These support AC-006 and provide
AC-008 groundwork. US-014.2 adds acknowledgment/visibility and the partial
US-014.3 phase adds conservative expired-claim failure. Tokens cannot stop stale scanner processes or guarantee exactly
one physical scan. See [Execution claims and leases](../execution-leases.md) for
defaults, transaction requirements, tradeoffs and verification.


## Production delivery composition (US-014.2)

`app/workers/main.py` polls one delivery at a time with a unique process identity.
`consumer.py` confirms saved message association and validates a terminal shape
before deleting a terminal duplicate. Nonterminal messages go through the existing
validated handoff and a one-attempt guarded claim. Its injected `Execution`
adapter extends SQS visibility before committing database renewal, then runs the
concrete adapter while a background heartbeat maintains authority. It interprets
and commits results/failures before fresh terminal confirmation and deletion.

A failed persistence transaction never deletes its receipt. A failed delete can
cause a terminal duplicate, which is confirmed without scanner invocation. Renewal
uncertainty or shutdown signals `ScanInterrupted` and leaves recovery state.
Malformed/mismatched/missing-job deliveries are left to the existing retry/DLQ
policy rather than acknowledged or used to reconstruct storage. Live competing
claims cannot launch another scanner. No physical exactly-once promise is added.

`app/repositories/recovery.py` locks up to ten expired leased ZAP jobs using
`SKIP LOCKED`, records a canonical recovery failure and clears authority. This
partial US-014.3 policy never automatically reruns an abandoned scan. Cancellation
requests and missing rows prevent reexecution; cancellation finalization and
retained deletion tombstones remain other stories. Privileged/manual recovery
and database restoration require separate operational decisions.

This reuses Idempotent Consumer with [Adapter](adapter.md) and
[Repository](repository.md), not a new pattern. Coordination adds database and
SQS traffic and dependency uncertainty can cause conservative failures. See
[Worker delivery](../worker-delivery.md) for timers, interactions, verification,
scaling, bounded-policy limits and pending acceptance. Earlier foundation sections
above describe their original callable scope; this section supplies the current
production composition.
