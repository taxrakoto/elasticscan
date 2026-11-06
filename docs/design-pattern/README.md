# Design patterns used in ElasticScan

This catalog explains the patterns used or proposed for ElasticScan. Each document
connects the pattern to a concrete project problem, its participating components,
tradeoffs, and verification. A directory or an idle service alone does not prove
that a pattern's application behavior has been implemented.

| Pattern | Application | Current status |
| --- | --- | --- |
| [Publish–Subscribe](publish-subscribe.md) | SNS subscriptions route scan messages to scanner-specific SQS queues | Local infrastructure implemented and routing verified; application publisher and consumer pending |
| [Transactional Outbox](transactional-outbox.md) | Commit jobs and publication intent together, then publish through the dispatcher | Job/intent storage implemented (US-004.1); acceptance transaction and publisher pending |
| [Idempotent Consumer](idempotent-consumer.md) | Handle repeated queue deliveries through durable worker claims and terminal-state protection | Proposed; application logic pending |
| [Dependency Injection](dependency-injection.md) | FastAPI supplies verified identity and configuration to team-context resolution | Implemented in US-002.1; test-only HTTP route exercises the dependency; production job routes pending |
| [Value Object](value-object.md) | Immutable `TeamContext` carries the authorized team and current caller | Implemented in US-002.1; used for job ownership persistence in US-002.2 |
| [Repository](repository.md) | Insert jobs with explicit verified ownership mapping | Creation implemented in US-002.2; team-scoped reads and atomic acceptance pending |

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
