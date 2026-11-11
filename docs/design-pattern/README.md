# Design patterns used in ElasticScan

This catalog explains the patterns used or proposed for ElasticScan. Each document
connects the pattern to a concrete project problem, its participating components,
tradeoffs, and verification. A directory or an idle service alone does not prove
that a pattern's application behavior has been implemented.

| Pattern | Application | Current status |
| --- | --- | --- |
| [Publish–Subscribe](publish-subscribe.md) | SNS subscriptions route scan messages to scanner-specific SQS queues | Local infrastructure implemented; US-006.2 adds isolated routing/configuration and reprovisioning tests; US-006.1 adds versioned publication and first-attempt dispatch; worker consumption pending |
| [Transactional Outbox](transactional-outbox.md) | Commit jobs and publication intent together, then publish through the dispatcher | Atomic acceptance in US-004.2; versioned publication in US-006.1; bounded retries, jittered backoff and expired-claim recovery in US-006.3; worker recovery pending |
| [Idempotent Consumer](idempotent-consumer.md) | Handle repeated queue deliveries through durable worker claims and terminal-state protection | US-008.1 guards, US-007.1 validation/handoff and US-014.1 renewable claim operations implemented; queue polling, periodic renewal, real execution, recovery and acknowledgment pending |
| [Adapter](adapter.md) | Separate scanner-specific execution from worker validation and database claims | US-007.1 implements a typed handoff contract with an injected test adapter; concrete ZAP implementation pending |
| [Dependency Injection](dependency-injection.md) | FastAPI supplies verified identity, policy, team context, and database factory | Production submission in US-003.2 and team-scoped retrieval in US-009.1; tests inject isolated storage and controlled external responses |
| [Value Object](value-object.md) | Immutable authorized context and acceptance snapshot | `TeamContext` implemented in US-002.1/US-002.2; `AcceptedScan` added in US-004.2 |
| [Repository](repository.md) | Insert jobs with verified ownership and read with authorized team filtering | Creation in US-002.2, atomic acceptance in US-004.2, team-scoped retrieval in US-009.1, guarded lifecycle writes in US-008.1 and execution leases in US-014.1; listing and other operations pending |

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
