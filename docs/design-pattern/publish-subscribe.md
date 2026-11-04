# Publish–Subscribe

**Status:** Local SNS/SQS infrastructure is implemented and routing has been
verified. Application publication and worker consumption remain pending feature
work. Production AWS behavior and IAM acceptance are not established by local
emulator tests.

## Problem and application

The publisher needs to announce a scan job without knowing which worker instance
will execute it. ElasticScan publishes to an SNS topic, and subscriptions route
messages into queues owned by scanner-specific workers.

```mermaid
flowchart LR
    D[Dispatcher] -->|Publish with scan_type attribute| SNS[SNS jobs topic]
    SNS -->|Subscription filter: zap| Q[ZAP SQS queue]
    Q --> W[ZAP worker instances]
    Q -->|Retry bound exceeded| DLQ[ZAP dead-letter queue]
```

SNS provides the publish–subscribe boundary. SQS buffers messages so worker
availability and execution speed are independent of publication. Multiple worker
instances consuming one queue compete for its messages; separate SNS subscriptions
can route copies to separate consumer queues.

## Current configuration

The bootstrap provisions `elasticscan-jobs`, a raw-delivery subscription filtered
on message attribute `scan_type=zap`, `elasticscan-zap`, and a dead-letter queue.
The ZAP queue's source policy restricts SNS publication to the configured topic.

See [provisioning](../../docker/localstack/provision.py),
[Compose](../../compose.yaml), and the
[bootstrap verification script](../../scripts/verify_local.py). The smoke check
publishes controlled ZAP and unrelated scan-type messages, then checks that only
the matching messages reach the configured queue.

## Tradeoffs and related patterns

Topic publication acceptance does not prove queue delivery or scan completion.
Delivery can be duplicated, and the design must not depend on global message
ordering. Operators need queue, subscription-delivery, and stalled-job monitoring.
Each added scan type needs a compatible message schema, routing attribute,
subscription filter, dedicated queue, and worker adapter.

[Transactional Outbox](transactional-outbox.md) makes the database-to-publication
handoff recoverable. [Idempotent Consumer](idempotent-consumer.md) handles repeated
deliveries at the worker. Neither is implemented merely by provisioning SNS/SQS.

This pattern supports FR-015–FR-017 and AC-003 in the
[requirements](../requirements.md). See [local development](../local-development.md)
for endpoint configuration and local verification boundaries.
