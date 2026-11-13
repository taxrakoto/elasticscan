# Design patterns used in ElasticScan

This catalog explains the patterns used or proposed for ElasticScan. Each document
connects the pattern to a concrete project problem, its participating components,
tradeoffs, and verification. A directory or an idle service alone does not prove
that a pattern's application behavior has been implemented.

| Pattern | Application | Current status |
| --- | --- | --- |
| [Publish–Subscribe](publish-subscribe.md) | SNS subscriptions route scan messages to scanner-specific SQS queues | Local infrastructure implemented; US-006.2 adds isolated routing/configuration and reprovisioning tests; US-006.1 adds versioned publication and first-attempt dispatch; US-014.2 adds sequential worker consumption in source |
| [Transactional Outbox](transactional-outbox.md) | Commit jobs and publication intent together, then publish through the dispatcher | Atomic acceptance in US-004.2; versioned publication in US-006.1; bounded retries, jittered backoff and expired-claim recovery in US-006.3; US-014.3 adds independently scheduled conservative expired-execution failure |
| [Idempotent Consumer](idempotent-consumer.md) | Handle repeated queue deliveries through durable worker claims and terminal-state protection | US-008.1 guards, US-007.1 validation/handoff and US-014.1 renewable claim operations implemented; US-014.2 adds polling, renewal, execution/outcome composition and acknowledgment; US-014.3 adds independent expiry-to-failure recovery; full cancellation/deletion APIs remain later stories |
| [Adapter](adapter.md) | Separate scanner-specific execution from worker validation and database claims | US-007.1 handoff, US-005.2 context setup and US-007.2 reusable bounded baseline execution; US-007.3 reusable ZAP outcome interpretation; US-014.2 adds live queue wiring and stop-event cleanup; full target-boundary acceptance pending |
| [Dependency Injection](dependency-injection.md) | FastAPI and internal callers supply verified context, policy, storage and scanner clients | Submission/retrieval implemented; US-005.2 application phase injects worker policy and scanner context client for isolated checks |
| [Value Object](value-object.md) | Immutable authorized context and acceptance snapshot | `TeamContext` implemented in US-002.1/US-002.2; `AcceptedScan` added in US-004.2; shared `SeverityCounts`/`ScanOutcome` added in US-007.3 |
| [Repository](repository.md) | Insert jobs with verified ownership and read with authorized team filtering | Creation in US-002.2, atomic acceptance in US-004.2, team-scoped retrieval in US-009.1, guarded lifecycle writes in US-008.1 and execution leases in US-014.1 and strict successful completion in US-008.2 and safe failure persistence in US-008.3 and lifecycle response serialization in US-009.2; listing and other operations pending |

## Maintaining the catalog

Use one Markdown document per pattern. Include:

- Status and the problem it solves in this project.
- The participating components, data flow, and relevant source paths.
- Tradeoffs, failure modes, and limits of the guarantees.
- Requirements, acceptance criteria, and implemented tests or pending verification.
- Related patterns and references where useful.

Update the documents alongside implementation changes. Keep proposed behavior
separate from implemented and verified behavior. Add patterns when they explain
an actual project design; empty package names are not implementation evidence.
Retry backoff and expiring claims are currently documented within the outbox
pattern because they support that workflow.

The [architecture overview](../architecture.md) describes how these components
fit together. For significant future choices, an Architecture Decision Record
(ADR) can record the decision, alternatives, and consequences; the pattern
document can then link to it. This catalog is not itself an approval log.

Documentation references: [Microsoft's ADR guidance](https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record)
and [AWS's transactional outbox guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).


## Partial local-slice verification

US-019.2's [partial integration checks](../local-slice.md) reuse
[Adapter](adapter.md#partial-local-integration-verification-us-0192) and
[Dependency Injection](dependency-injection.md#partial-local-integration-verification-us-0192)
to join production functions with real PostgreSQL, Keycloak and LocalStack and a
simulated scanner. This is test-only composition; the deployed worker remains
idle and full target-boundary and end-to-end acceptance remain pending.


## Worker delivery integration

[Worker delivery](../worker-delivery.md) documents the US-014.2 composition of
[Idempotent Consumer](idempotent-consumer.md), [Adapter](adapter.md) and
[Repository](repository.md), plus the conservative US-014.3 recovery
policy. Existing running containers are unchanged until explicitly recreated.
Heartbeat/stop-event/locking mechanisms are coordination techniques. US-017.1 budgets are documented below; full cancellation/deletion APIs,
load benchmarking and target-boundary acceptance remain separate.


## Independent recovery and execution budgets

US-014.3 extends [Repository](repository.md) and
[Idempotent Consumer](idempotent-consumer.md) with independent recovery scheduling,
configurable batch/interval and one automatic execution attempt followed by safe
failure. This supersedes the earlier partial recovery status above. US-017.1
exposes provisional Compose concurrency/CPU/memory budgets; runtime timeout and
cleanup reuse the existing adapter. See [execution budgets](../execution-budgets.md).
Full cancellation/deletion APIs, tombstones and US-017.3 load acceptance remain
separate; present recovery preserves cancellation metadata and absent records.


## Worker failure classification and bounded retries

US-015.1 extends [Idempotent Consumer](idempotent-consumer.md) with bounded retries
at receive, fresh terminal-read and acknowledgment boundaries. Retry/backoff are
resilience techniques; scanner invocation and uncertain transaction commits are
not repeated. See [worker retries](../worker-retries.md) for classification,
configuration, supervision limits, diagnostics and verification.
