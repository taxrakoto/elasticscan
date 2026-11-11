# Job publication (US-006.1 and US-006.3)

The dispatcher reads committed jobs and dispatch intents, publishes to SNS, and
records publication state. It runs independently of the HTTP API, from the same
application image. It does not execute scans or change scan status.

## Version-one contract

`app/schemas/job_message.py` defines `JobMessage`. The shared fixture is
[`job-message.json`](../tests/fixtures/contracts/v1/job-message.json):

```json
{
  "schema_version": 1,
  "job_id": "123e4567-e89b-42d3-a456-426614174000",
  "scan_type": "zap",
  "parameters": {"url": "https://approved.example.invalid/"}
}
```

Only version 1 and `zap` are supported. Required fields, UUID and HTTP/HTTPS URL
syntax are validated, and extra fields are rejected, including nested parameters.
Parameters come from immutable saved job fields, not a new client request. The
publisher does not redo DNS/target authorization; submission already applies the
initial-target policy. Workers must later verify messages against stored jobs
and enforce execution-time target controls. This fixture supplies the missing
job-message agreement from US-003.1; it does not complete that story's other
cross-component contracts or change the public submission JSON.

SNS receives `scan_type` as a String message attribute with the same value as the
body. The existing raw-delivery subscription routes `zap` to its dedicated queue.

## One publication operation

`app/dispatchers/publication.py` exposes
`publish_job(sessions, *, job_id, sns, topic_arn, first_attempt_only=False)`.
The caller supplies a SQLAlchemy session factory, a saved job UUID, an SNS client,
and an operator-controlled topic ARN. It returns:

| Outcome | Meaning |
| --- | --- |
| `published` | SNS returned a message ID and the publication timestamp committed |
| `failed` | Safe failure code and next retry time committed; intent remains unpublished |
| `exhausted` | Final failed attempt committed; automatic publication stops without deleting intent |
| `invalid` | Saved payload failed contract validation; no SNS call occurred |
| `skipped` | Intent was missing, locked, ineligible, or already processed |
| `claim_lost` | Another claim or suppression prevented this attempt's final update |

The first short transaction locks only the intent using `FOR UPDATE SKIP LOCKED`.
It requires a pending job without cancellation-request metadata, an unsuppressed,
unpublished due intent, no active claim, fewer than five attempts, and no saved
`invalid_job_message` error. It validates the saved message, increments
`attempt_count`, and commits a random claim token with a 60-second lease.
Before contacting SNS it also saves a crash-recovery time at lease expiry plus
backoff. Invalid messages instead commit `invalid_job_message` without a claim
and are excluded from future automatic attempts. An attempt count measures
committed claims, including interrupted attempts, not confirmed network sends.

SNS is called after that transaction closes. A second short transaction records
the result only while the same claim token still owns an unpublished,
unsuppressed intent. Success sets a UTC `published_at`; both success and failure
clear the claim. SDK errors store `sns_publication_failed`; missing message IDs
store `sns_acknowledgement_missing`. Raw exception details, credentials and target
parameters are not logged. Publication outcome logs include `job_id`,
`dispatch_outcome`, `attempt_count`, `next_attempt_at`, and `last_error_code`.
The process formatter renders these as job ID, outcome, attempt, next and error.
Only a committed retryable failure logs an upcoming retry time.

Database exceptions propagate from `publish_job`; no successful result is
returned before commit. If SNS accepted a message but acknowledgment or the
database update is lost, the outcome is uncertain and a later attempt may
duplicate delivery. SNS acknowledgment does not prove queue delivery or scan
completion. A claim token protects database updates; it cannot retract messages.

## Automatic bounded recovery

`app/dispatchers/main.py` selects at most ten eligible intents per pass, ordered
by retry time, creation time and ID, and waits two seconds between passes.
Discovery and atomic claiming share `dispatch_conditions()`, so stale discovery
cannot bypass the due time, active lease, invalid-message exclusion or attempt
budget. Multiple processes coordinate through PostgreSQL row locks and tokens.

`MAX_ATTEMPTS=5` and `LEASE_SECONDS=60` are code defaults. There are five total
attempts, not five additional retries. `retry_delay()` uses capped exponential
backoff with uniform jitter (random variation). The four retry delays are 1–2,
2–4, 4–8 and 8–16 seconds. Its ceiling is 30 seconds; no sixth attempt is allowed.
Actual publication can occur later because of polling, other jobs and service
latency. The SDK makes only one request attempt per dispatcher claim.

A handled SNS failure releases the claim and schedules backoff from the database
clock at finalization. An interrupted claim remains recoverable only after both
its lease and saved retry time expire. A restart does not reset the budget.
`first_attempt_only=True` remains available to trusted callers that need the
additional zero-attempt guard; the automatic dispatcher no longer uses it.
All callers still obey the shared retry budget and eligibility rules.

The process requires `SNS_TOPIC_ARN`, uses `SNS_ENDPOINT_URL` when supplied, and
uses the existing AWS credential/region configuration. Its SNS client sets:

```python
Config(
    connect_timeout=3,
    read_timeout=5,
    retries={'total_max_attempts': 1},
)
```

These are connection/read timeouts, not an absolute end-to-end deadline. The
lease can expire during unusually slow execution; token checks protect later
database updates. Callers constructing their own clients should use equivalent
bounded configuration. No database row lock is held during SNS publication.

SIGTERM/SIGINT stop new work and allow the current call/finalization to finish.
The client closes, connection pool is disposed, and `/tmp/dispatcher-ready` is
removed. That file is a startup/liveness marker, not dependency readiness or a
guarantee that jobs are progressing. Database failures produce a safe process
log and another discovery pass after the normal wait. Unexpected errors terminate
the process; Compose restarts it with `unless-stopped`.

Failed, invalid, interrupted and exhausted attempts remain stored for operator
inspection. The last handled failure returns and logs `exhausted`. If the final
attempt crashes, exhaustion is derived from the saved counter once its lease
expires; no final log can be guaranteed from a crashed process. Neither case
changes the scan's `pending` status or marks the intent published/suppressed.
A saved next-attempt timestamp does not authorize another attempt after budget
exhaustion. Scan cancellation can race publication; these eligibility checks are
not complete cancellation semantics. Workers must verify durable state before
executing.

## Inspecting stalled publication

Run this read-only query in the intended application database/schema. It exposes
safe dispatch metadata without target parameters. The bound of five must match
`MAX_ATTEMPTS` if that code default changes.

```sql
SELECT
    d.job_id,
    j.status AS job_status,
    d.attempt_count,
    d.last_error_code,
    d.next_attempt_at,
    d.lease_expires_at,
    CASE
        WHEN d.suppressed_at IS NOT NULL THEN 'suppressed'
        WHEN d.last_error_code = 'invalid_job_message' THEN 'invalid'
        WHEN d.claim_token IS NOT NULL
             AND d.lease_expires_at > clock_timestamp()
            THEN 'publishing'
        WHEN d.attempt_count >= 5 THEN 'exhausted'
        WHEN j.status <> 'pending'
             OR j.cancellation_requested_at IS NOT NULL
            THEN 'ineligible'
        WHEN d.next_attempt_at > clock_timestamp() THEN 'waiting'
        ELSE 'due'
    END AS publication_state
FROM dispatch_intents AS d
JOIN jobs AS j ON j.job_id = d.job_id
WHERE d.published_at IS NULL
ORDER BY d.created_at, d.id;
```

For waiting/due records, inspect dispatcher logs by job ID and check database,
SNS endpoint, credentials and topic configuration. Restore the cause and allow
the saved schedule to run. For invalid/exhausted records, preserve the counter,
error and lease diagnostics, correct the cause, then prepare a separately
reviewed recovery for that same job. Confirm no live publisher owns it and
account for possible earlier delivery before granting any new attempt budget.
Do not recreate the accepted job as a substitute for recovering its intent.
This story adds no automatic budget reset, operator mutation command, alerting
service or dead-letter reconciliation. Prolonged outages can exhaust the budget
and require operator action; query/log visibility is not proactive notification.

## Operation and verification

See [local development](local-development.md#us-0061-dispatcher-process) for the
separate container and startup commands. Deploy migrations before starting it.
The API accepts jobs without waiting for SNS. Source changes take effect in
application containers only after a rebuild/recreation; mounted-source tests do
not deploy them. The worker remains idle.

See [test instructions](../tests/README.md#us-0061-versioned-publication-and-dispatcher)
for offline checks, isolated PostgreSQL recovery checks, LocalStack access
interruption/restoration and regression commands. Tests never consume application
queues. The LocalStack scenario uses an unreachable endpoint for an isolated
process, then restarts it with the real emulator endpoint and the same saved job.
This tests access recovery and process restart, not a shared SNS service outage.
A separate PostgreSQL test injects finalization failure after mocked SNS success
and verifies that recovery can publish the identical message twice.

The tests cover recoverable intent (FR-014/AC-007), versioned publication
(FR-015), and local AC-003 with US-006.2's attribute-filter tests (FR-016).
Existing submission tests cover the initial-persistence-failure HTTP 503 part of
AC-007. Real AWS IAM, API crash demonstrations and worker/scanner end-to-end
execution remain separate verification. SNS recovery succeeds automatically
within the remaining retry budget; success through arbitrary outages is not
promised.

The reused [Transactional Outbox](design-pattern/transactional-outbox.md) and
[Publish–Subscribe](design-pattern/publish-subscribe.md) patterns explain the
handoff and routing. Backoff, leases and conditional writes are reliability and
concurrency techniques; Pydantic and SQLAlchemy are libraries.
