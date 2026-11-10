# Job publication (US-006.1)

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
| `failed` | The attempt's safe failure code committed; intent remains unpublished |
| `invalid` | Saved payload failed contract validation; no SNS call occurred |
| `skipped` | Intent was missing, locked, ineligible, or already processed |
| `claim_lost` | Another claim or suppression prevented this attempt's final update |

The first short transaction locks only the intent using `FOR UPDATE SKIP LOCKED`.
It requires a pending job without cancellation-request metadata, an unsuppressed,
unpublished due intent, and no active claim. It validates the saved message,
increments `attempt_count`, and commits a random claim token with a 60-second
lease. Invalid messages instead commit `invalid_job_message` without a claim.
An attempt count measures claimed attempts, not confirmed network sends.

SNS is called after that transaction closes. A second short transaction records
the result only while the same claim token still owns an unpublished,
unsuppressed intent. Success sets a UTC `published_at`; both success and failure
clear the claim. SDK errors store `sns_publication_failed`; missing message IDs
store `sns_acknowledgement_missing`. Raw exception details, credentials and target
parameters are not logged. Publication outcome logs include `job_id` and
`dispatch_outcome`; the process formatter renders both fields.

Database exceptions propagate from `publish_job`; no successful result is
returned before commit. If SNS accepted a message but acknowledgment or the
database update is lost, the outcome is uncertain and a later attempt may
duplicate delivery. SNS acknowledgment does not prove queue delivery or scan
completion. A claim token protects database updates; it cannot retract messages.

## Automatic first attempts

`app/dispatchers/main.py` selects at most ten eligible intents per pass, oldest
first, and waits two seconds between passes. Both discovery and the atomic claim
require `attempt_count=0`, preventing a stale discovery result from retrying a
failed attempt. Multiple dispatcher processes coordinate through PostgreSQL.

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

Failed, invalid, or interrupted attempts remain stored for operator inspection.
The process does not automatically retry them or reclaim expired attempted work.
Bounded retries, backoff, exhaustion handling and outage/crash recovery remain
US-006.3. Direct trusted calls without `first_attempt_only=True` can attempt an
eligible saved intent again, but are not a recovery scheduler. Scan cancellation
can race publication; these eligibility checks are not complete cancellation
semantics. Workers must still verify durable state before executing.

## Operation and verification

See [local development](local-development.md#us-0061-dispatcher-process) for the
separate container and startup commands. Deploy migrations before starting it.
The API accepts jobs without waiting for SNS. A worker is not required for
publication; its implementation remains separate work.

See [test instructions](../tests/README.md#us-0061-versioned-publication-and-dispatcher)
for offline tests, isolated PostgreSQL publication/claim checks, LocalStack
delivery, and the full regression command. Tests never consume application queues.
They establish FR-015 and local application publication toward AC-003, together
with US-006.2's unrelated-attribute exclusion tests. They establish preservation
and observable failure toward FR-014/AC-007, not complete automatic recovery.
Real AWS IAM, worker execution, full AC-007 and end-to-end acceptance remain
separate verification.

The reused [Transactional Outbox](design-pattern/transactional-outbox.md) and
[Publish–Subscribe](design-pattern/publish-subscribe.md) patterns explain the
handoff and routing. Leases/conditional writes are concurrency techniques;
Pydantic and SQLAlchemy are libraries, not separate design patterns.
