# Dead-letter delivery and observation (US-015.2)

The local provisioning source configures a source queue and dead-letter queue
(DLQ). The operator command samples the DLQ without scanner execution, database
writes, message deletion or redrive. This implements delivery-bound observation;
job reconciliation and controlled recovery remain US-015.3. Updating source or
building an image does not change existing queues or running containers.

## Configuration and managed attributes

| Setting / attribute | Default | Meaning |
| --- | --- | --- |
| `SQS_MAX_RECEIVE_COUNT` | 3 | Source delivery bound, project range 1–100 |
| `SQS_DLQ_URL` | Local `elasticscan-zap-dlq` URL in `.env.example` | Explicit target for the observation command |
| Source retention | 345600 seconds | Four days |
| DLQ retention | 1209600 seconds | Fourteen days |
| `RedriveAllowPolicy` | `byQueue` | Only the provisioned source ARN may use this DLQ |

`docker/localstack/provision.py:provision` validates the bound before service
calls and rejects identical source/DLQ queue names. It creates or finds queues,
then explicitly applies its managed attributes, including redrive policy,
retention and the SNS source policy. Running it again repairs changed managed
attributes while retaining resource identity and existing subscription deduplication.
It is not a transaction across services: failure can leave partially applied
configuration, requiring inspection and another provisioning pass.

The LocalStack ready-hook receives `SQS_MAX_RECEIVE_COUNT` through Compose. The
existing bootstrap verifier now checks the configured bound instead of hardcoding
three. `.env.example` documents new settings; an existing private `.env` is not
rewritten. The default still applies when the bound is absent. Applying changes
to an existing default queue requires a deliberate provisioning operation; the
isolated tests do not run that operation against application queues.

Source deliveries, infrastructure operation attempts and scanner execution
attempts are different counters. US-015.1's retry policy does not alter SQS
redrive counts. SQS moves exhausted messages to the DLQ through redrive; the worker
does not manually forward poison messages. A message's usable job ID may be
absent, and a poison message does not imply any scanner attempt took place.

AWS recommends longer DLQ retention for standard queues because original enqueue
time continues to determine message expiration. Fourteen-day DLQ retention is
not fourteen extra days after transfer. Refer to
[AWS dead-letter guidance](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
and [queue attributes](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SetQueueAttributes.html).
LocalStack policy/configuration checks are not real-AWS IAM enforcement acceptance.
A production deployment must establish corresponding infrastructure and IAM.

## Operator command and permissions

After building an image containing the new module, run from `elasticscan/`:

```sh
docker compose run --rm --no-deps -e SQS_DLQ_URL=http://localstack:4566/queue/us-east-1/000000000000/elasticscan-zap-dlq --entrypoint python api -B -m app.maintenance.dead_letters --limit 10
```

This example explicitly supplies the DLQ URL because older private `.env` files
will not contain it. `SQS_QUEUE_URL` supplies the source queue. The endpoint and
credentials come from existing deployment configuration. For AWS, use the actual
queue URLs/endpoint configuration and authorized credentials rather than local
example URLs. Required permissions are `sqs:GetQueueAttributes` on source and DLQ,
and `sqs:ReceiveMessage` on the DLQ. This command requires no delete, send, redrive
or database privileges. Attribute validation is an accidental-misconfiguration
guard, not authentication or a substitute for IAM.

`observe_dead_letters` first checks the source's redrive target, the DLQ ARN and
its explicit byQueue allow policy. Mismatch prevents receiving. It then performs
one short-poll receive for 1–10 messages, using a 30-second visibility timeout.
Sampled messages become temporarily invisible and their receive counts increase;
this is not a side-effect-free peek. Short polling can return no messages even
when approximate depth is nonzero. No exhaustive audit, scheduling or automatic
continuous DLQ monitoring is introduced. Repeat invocations can observe the same
message. Do not invoke it expecting to drain or repair the queue.

The CLI exits 0 for a completed observation, including an empty sample, or 1 for
known configuration/service errors. Argument-parser errors use its normal exit
code. Failures emit a fixed safe event without raw exceptions. API-image
packaging includes the maintenance module; it is not added to the scanner image.

## Safe diagnostics

Each output line is JSON. `dead_letter_depth` contains approximate `visible` and
`inflight` counts sampled before receive. `dead_letter_observed` contains only:

- Canonical UUID `job_id`, or `-` when unusable.
- Canonical UUID `message_id`, or `-` when unusable.
- `schema_valid`, using the shared version-one JobMessage validation rules.
- `dlq_receive_count`, sanitized from the received system attribute.

The job ID is an **untrusted correlation hint** from the message. The command does
not establish ownership, authenticate a caller for that job, look up a database
record or confirm that one exists. Schema-valid messages can still be inconsistent
with stored jobs. `dlq_receive_count` is not a preserved source exhaustion count;
it is the received observation attribute and must not be treated as proof of how
many source attempts occurred. Invalid/missing diagnostic counts normalize to zero.
Raw bodies, targets, parameters, credentials, receipt handles and validation error
texts are neither returned nor logged. See the supported
[ReceiveMessage attributes](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_ReceiveMessage.html).

## Investigation procedure

1. Inspect approximate DLQ depth and take a bounded diagnostic sample.
2. Use safe message/job identifiers to correlate with restricted operational logs
   and persisted jobs through already authorized access. Treat unknown IDs as
   unverified; a missing record must not be recreated from message parameters.
3. Check worker health, queue configuration and dependency incidents. Preserve
   terminal outcomes, cancellation requests, team ownership and deletion controls.
4. Leave messages in the DLQ while investigating. Do not reset attempts, manually
   invoke scanners or bulk-redrive as part of this observation task. Controlled
   reconciliation/redrive requires US-015.3 and its lifecycle checks.

No DLQ alarm, metrics exporter, database finalizer or redrive command is added.
The command provides structured diagnostics and depth sampling, not full NFR-015
monitoring or full FR-023/AC-009 reconciliation acceptance.

## Patterns and verification

This extends [Publish–Subscribe](design-pattern/publish-subscribe.md) with service
managed failure routing. Redrive/retention policies and bounded observation are
infrastructure techniques, not new design patterns. Benefits are repeatable local
configuration and safer diagnostics; tradeoffs include extra queue calls, temporary
invisibility, duplicate observations, retention limits and partial provisioning.

Unit tests cover safe fields, valid/invalid bodies, limits, queue relationship
validation, nondeletion and invalid provisioning. Integration checks provision
unique LocalStack resources, repair deliberately altered attributes and drive a
poison delivery through the production consumer for each configured source
receive. They then observe it in the DLQ and verify job-correlated diagnostics,
no scanner construction, no acknowledgment and unchanged pending job/attempts.
PostgreSQL schemas and queues/topics are disposable. See the
[test guide](../tests/README.md) for actual commands and results.
