# Worker failure classification and retries (US-015.1)

The worker source retries explicitly selected repeatable infrastructure operations
within configured limits. Updated source or a built image does not replace a
running container. Scanner failures still use existing safe outcome persistence;
this policy never wraps scanner invocation or reopens an execution claim.

## Configuration and attempt scopes

| Setting | Default | Accepted values |
| --- | --- | --- |
| `WORKER_RETRY_MAX_ATTEMPTS` | 3 | Integer 1–10, including the first call |
| `WORKER_RETRY_BASE_DELAY_SECONDS` | 2 | Integer 1–30 |

`WorkerSettings` validates these at startup and reads environment overrides.
Missing settings in an existing private `.env` use defaults. That file is not
rewritten. Delays are `min(base * 2 ** (attempt - 1), 60)` seconds after a failed
attempt, with no delay after the final failure. Defaults therefore mean at most
three calls and waits of two and four seconds. Attempts of one disable retries.
Operation duration adds to those waits; the existing SDK timeouts still apply.
The SQS client uses `total_max_attempts=1`, avoiding hidden SDK retry multiplication
in the production worker. Database connection preparation may add its own work.

This counter is local to one operation invocation. Successful completion, a later
message delivery, another confirmation call or process restart gets a new budget.
The fixed one-scanner-attempt cap is separately enforced by the database claim.
The queue receive/redrive count is another independent bound. Compose currently
uses `restart: unless-stopped`: consumer exhaustion stops the current process,
but the supervisor can restart it with a new operation budget. This is not a
durable job-wide infrastructure retry counter or a permanent service shutdown.

## Classification

`app/workers/retry.py:retry_category` allows only these transient categories:

| Category | Eligible failures |
| --- | --- |
| `queue_connection` | Botocore connection closed, connect/read timeout, endpoint connection failure |
| `queue_service` | Approved throttling, request-timeout, internal/service-unavailable codes, or HTTP 429/500/502/503/504 |
| `database_connection` | SQLAlchemy OperationalError with no SQLSTATE, connection-class SQLSTATE beginning `08`, or `40001`, `40P01`, `55P03`, `53300`, `57P01`, `57P02`, `57P03` |

Operational errors without a SQLSTATE are conservatively eligible for bounded
retry at the selected read-only boundary; they are not proven transient.
Authentication (`28P01`), schema (`42P01`), integrity errors, missing credentials,
access denied and unknown exceptions do not qualify. Classification is an explicit
project policy, not a guarantee that an eligible failure will recover. Unknown
programming exceptions propagate rather than becoming public scanner errors.

## Operation boundaries

1. `main.py:consume_loop` wraps SQS receive. Exhaustion or a permanent known
   dependency/validation error ends the consumer; `run` stops its recovery thread.
2. `consumer.py:consume_one` wraps each terminal confirmation. Every attempt calls
   `terminal_matches`, which opens a fresh session and checks job ID, scanner,
   parameters, terminal state and the lifecycle schema. Only a successful matching
   confirmation authorizes message deletion.
3. Acknowledgment wraps SQS delete using the current receipt. A lost response can
   leave uncertain delivery state; retry is safe for scanner execution because a
   durable terminal job already exists. An invalid/stale receipt is not blindly
   classified as transient. Later redelivery reconfirms state without scanning.

Claim commits and outcome-write transactions are not automatically repeated.
An uncertain write leaves the message for conservative recovery or later terminal
confirmation. Lease/visibility renewal uncertainty still stops scanning immediately;
retry waits must not extend uncertain scanner authority. The existing recovery
scheduler is recurring maintenance, with its own interval, rather than an
operation wrapped by this policy. Publication retains US-006.3's durable outbox
retry policy; this change does not replace it.

## Permanent scanner outcomes

Timeout, execution errors and invalid output remain non-retryable scanner outcomes.
`execute_zap_scan` normalizes them and `fail_job` stores a fixed safe error, null
result and finish timestamp under the current claim token. Durable confirmation
then permits acknowledgment. Infrastructure retries never call the adapter.
A duplicate delivery observes the terminal outcome; it does not rerun the scanner.

## Diagnostics and operator response

`worker_retry` logs operation, optional job ID, category, failed attempt and next
delay. `worker_retry_exhausted` records total attempts; non-retryable errors emit
`worker_operation_not_retryable`. The helper does not log exception text, SQL,
credentials, target URLs, scanner reports or receipt handles. Receive failures
have no job identity and use `job_id=-`. Logs use stable key/value fields within
the existing formatter; a metrics backend is not added.

On exhaustion, inspect operation/category/attempts and dependency health. Repair
credentials or permissions for permanent failures; restore connectivity/capacity
for outages. Verify worker restart and subsequent queue progress. For jobs with
uncertain claims or writes, inspect their persisted state and recovery logs.
Do not reset attempts or reopen terminal jobs to force a scan. Follow
[execution budgets](execution-budgets.md) for abandoned-execution recovery.
DLQ identification and controlled redrive remain US-015.2 and US-015.3.

## Patterns, tradeoffs and verification

This extends [Idempotent Consumer](design-pattern/idempotent-consumer.md): guarded
claims and durable terminal confirmation make these selected repeated operations
safe for logical execution. Bounded retry and exponential delays are resilience
techniques, not additional design patterns. Deterministic waits are easy to audit
but may align across workers. Retries add dependency calls and delay failure
reporting. Fresh sessions avoid retrying a failed database transaction in place.

Unit checks verify classification, attempt/delay limits, shutdown, safe logs,
configuration and consumer exhaustion. Real PostgreSQL/LocalStack integration
checks use injected connection failures at production operation boundaries to
verify successful delete retry, fresh confirmation, permanent scanner failure
and exhausted acknowledgment followed by terminal replay. They do not simulate a
whole network outage or launch a real scanner. See the [test guide](../tests/README.md).

This implements the failure-classification and operation-retry part of FR-022.
Full FR-023/AC-009 reconciliation and dead-letter observability remain subsequent
stories; broader NFR-015 metrics and production runbooks are not claimed.


## Queue-level failure routing (US-015.2)

[Dead-letter delivery](dead-letter-delivery.md) adds configurable SQS source
receive limits, repeatable queue-attribute repair and bounded DLQ observation.
These delivery counts are separate from this document's per-operation retry
budget and the job's one-scanner-attempt cap. Operator sampling keeps messages
in the DLQ and does not perform reconciliation/redrive. US-015.3 remains separate.
