# Worker delivery and conservative recovery

US-014.2 connects SQS delivery, the existing validated worker handoff, bounded ZAP
execution, outcome interpretation, guarded persistence and acknowledgment.
US-014.3 adds independent conservative recovery; US-017.1 adds configurable
[execution budgets](execution-budgets.md). US-005 remains unchanged. Updated
source/images do not replace already running containers.

## Components and delivery flow

`app/workers/main.py` runs one sequential consumer per process. Each process has
a hostname/UUID worker ID. Multiple containers can share the ZAP queue; database
claims serialize competing starts for the same job. There is no global scan cap.

1. Start independent background recovery through `app/workers/recovery.py`.
   Each pass commits a configurable batch through
   `app/repositories/recovery.py:fail_expired_claims`; scanning does not block it.
2. Receive at most one delivery with a ten-second long poll and the configured
   visibility timeout. Stop signals prevent subsequent claims/polls.
3. `consumer.py:consume_one` parses the shared message and checks stored scan
   type and complete parameters before confirming an existing terminal outcome.
   `JobStatusResponse` validates the stored lifecycle/result/error shape. Only a
   matching, valid terminal outcome permits acknowledgment without execution.
4. For nonterminal work, `zap.py:handle_message` retains job/message comparison,
   current target policy and atomic claim checks. The live consumer explicitly
   passes `max_attempts=1`. Commit succeeds before scanner invocation.
5. `consumer.py:Execution` extends SQS visibility before renewing the same
   database token/worker lease. A successful renewal transaction is required
   before creating the concrete scanner adapter. A background thread repeats
   renewal while the main thread executes and interprets the scan.
6. On a successful execution, atomically commit counts/status/timestamps through
   `complete_job`. On a scanner failure, use the existing canonical `fail_job`.
   Terminal writes require the same strictly unexpired claim.
7. Read and validate the matching terminal state in a new session, then delete
   the current SQS receipt. A rejected/uncertain terminal commit never reaches
   deletion. A failed delete leaves the terminal state for safe redelivery.

The consumer wraps the existing Adapter interface rather than putting SQS
receipts into `ScanRequest`. The raw handoff still returns `invoked` rather than
claiming completion. In production its injected `Execution` adapter now performs
renewal, interpretation and guarded persistence before returning.

## Provisional coordination settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `WORKER_LEASE_SECONDS` | 60 | Authority window for a current execution claim; maximum 3600 |
| `WORKER_HEARTBEAT_SECONDS` | 15 | Renewal interval; at most one third of the lease |
| `WORKER_VISIBILITY_SECONDS` | 90 | Delivery invisibility; at least lease plus two heartbeat intervals; maximum 43200 |

`WorkerSettings` validates positive integer values at startup. Missing variables
use defaults; `.env.example` documents them. The private `.env` is not rewritten.
SQS visibility is extended first so it covers the subsequently renewed lease.
The worker uses bounded SDK retries/timeouts, PostgreSQL connection/statement/
lock timeouts and Linux TCP failure detection. These help dependency failure and
shutdown handling; they do not prove real-AWS timing or API benchmark acceptance.

Existing `ZAP_CRAWL_MINUTES=1`, `ZAP_TOTAL_TIMEOUT_SECONDS=900`, CPU 1.0 and memory
2 GiB remain provisional settings. Compose now exposes CPU, memory and replica
settings.
See [execution budgets](execution-budgets.md) for enforcement and verification.

## Interruption and recovery policy

`ScanInterrupted` is distinct from scanner failure. The baseline adapter polls
its process in intervals up to 0.2 seconds when given a stop event. Its existing
`finally` kills the scanner process group and removes temporary artifacts. The
outcome interpreter lets interruption propagate rather than storing a fabricated
scanner error. Shutdown, cancellation observation or uncertain renewal sets
that event. After uncertainty, no terminal result is stored by that execution.

This phase permits **one automatic execution attempt per job**, not automatic
scanner retries. An available consumer finds expired, leased, running ZAP jobs
and commits `failed` with the canonical `recovery_exhausted` error. It clears
token/expiry, preserves ownership and diagnostic metadata, and never reopens
terminal state. Row locks use `SKIP LOCKED`, allowing concurrent recovery passes
without waiting on another worker's claimed rows. Callers own commit/rollback.

Recovery runs in a separate thread, with separate sessions, even during an active
scan. Passes default to ten jobs followed by a five-second interruptible wait.
A large backlog, row contention or dependency/worker outages can delay
reconciliation; no fixed end-to-end recovery SLA during outages is claimed. A hard
crash cannot run local cleanup. Restored workers can fail its expired claim,
while old writers are fenced by token/expiry. Physical exactly-once scanning is
not promised; database commit cannot atomically control external scanner work.

Cancellation requests exclude jobs from new claims and expiry-to-failure writes.
Observed requests interrupt a current scan but this phase does not finalize
`cancelled`. Such interrupted requested jobs retain their request for the later
cancellation implementation. Missing jobs are never reconstructed from messages;
full deletion tombstones remain US-013.2. Legacy running rows without leases need
operator handling. An exhausted pending row cannot acquire another live-worker
claim; automatic reconciliation of manually altered pending rows is not added.

## Failure handling

| Condition | Outcome |
| --- | --- |
| Malformed/versioned/mismatched message, missing job, denied target | No scanner start or acknowledgment; normal queue retry/redrive policy applies |
| Another valid claim | No competing scan; delivery remains recoverable |
| Initial/periodic renewal fails or becomes uncertain | Stop/avoid scanner; no terminal write or acknowledgment from that execution |
| Safe scanner timeout/error | Commit canonical failure, confirm it, then acknowledge |
| Terminal persistence fails or ownership is lost | No acknowledgment; expiry recovery or later terminal confirmation applies |
| Delete fails after commit | Preserve outcome; later delivery confirms it without rerunning |
| Shutdown | Stop new work, interrupt active scanner, leave recoverable claim/delivery |
| Expired claim without cancellation | Commit safe recovery failure; never automatically rescan |
| Incompatible stored terminal outcome | Validation fails; leave delivery undeleted |

The existing queue's three-receive DLQ policy is unchanged. Invalid or repeated
competing deliveries can reach that DLQ. There is no DLQ consumer or automatic
redrive in this phase. Durable expired-job failure does not depend on the
original delivery still being in the main queue.

## Activate and scale deliberately

Apply migrations before starting the dispatcher/worker. Configure only a target
you are explicitly authorized to scan; empty target configuration denies starts.
Full US-005 redirect/DNS/egress enforcement remains pending. From `elasticscan/`:

```sh
docker compose exec -T api alembic upgrade head
docker compose build zap-worker
docker compose up -d --no-deps --scale zap-worker=3 zap-worker
docker compose logs --tail 100 zap-worker
docker compose stop zap-worker
```

Each replica handles one scan at a time with its own CPU/memory limits. The worker
has a 30-second Compose shutdown grace period and `unless-stopped` restart policy.
`/tmp/worker-ready` indicates process startup only. It does not establish database
readiness, queue progress or target enforcement. This implementation's checks do
not recreate the running worker against application queues. Bootstrap messaging
verification must run with active consumers stopped because it shares the
default queue; use isolated integration tests for consumer verification.

## Patterns, verification and acceptance

This reuses [Idempotent Consumer](design-pattern/idempotent-consumer.md),
[Adapter](design-pattern/adapter.md) and [Repository](design-pattern/repository.md).
Heartbeats, stop events, conditional writes and row locks are implementation
techniques. More coordination adds database/SQS calls; uncertain acknowledgment
can cause redelivery and uncertainty deliberately favors stopping over continued
execution. No token, target URL, report or exception body is logged by the loop.
Complete correlated audit logging remains US-018.1.

New unit/integration checks cover timing validation, parsing, interruption,
shutdown-before-poll, real visibility/lease renewal, durable outcomes, terminal
redelivery, failed persistence, renewal uncertainty, conservative recovery,
cancel-request preservation, stale writers, competing consumers and the actual
queue loop. Isolated real-ZAP tests check process/temporary-file cleanup on
interruption as well as completion and timeout. See the
[test guide](../tests/README.md#us-0142-delivery-and-us-0143-partial-recovery).

US-014.2 supports FR-019/FR-021 and AC-006/AC-008's delivery/storage behavior.
The US-014.3 conservative policy is one automatic attempt followed by failure
after abandonment. Additional tests kill a worker process after a committed claim
and wait for natural expiry; they also cover bounded batches, competing recovery,
rollback and recovery while another scanner is busy. Full cancellation/deletion
APIs and retained tombstones remain later stories: current checks preserve
requests and never recreate missing rows. US-017.3 retains benchmark acceptance.
No global concurrency limiter, migration, API endpoint, automatic scanner retry
or AWS deployment is introduced.


## Bounded infrastructure operations (US-015.1)

[Worker retries](worker-retries.md) adds failure classification and bounded retry
for receive, fresh terminal confirmation and acknowledgment. Defaults are three
total calls with two/four-second waits. Known permanent errors stop immediately;
consumer dependency exhaustion returns rather than repeating the old fixed wait.
The existing Compose restart policy can start a new process and operation budget.
Claims, scanner execution, outcome writes and renewal uncertainty retain their
conservative behavior. One scanner attempt, per-operation retries and SQS delivery
counts are separate bounds. No queue redrive policy or public error contract changes.
