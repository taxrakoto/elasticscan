# ElasticScan architecture

ElasticScan separates the HTTP API, PostgreSQL persistence, message dispatch,
and scanner execution into independent responsibilities. SNS routes messages to
scanner-specific SQS queues; workers execute scans and persist their outcomes.

The current implementation is a local bootstrap: the API exposes liveness and
OpenAPI, PostgreSQL and messaging infrastructure are provisioned, and Keycloak
provides development identities for two teams. Dispatcher and worker processes
remain idle placeholders. SQLAlchemy job and dispatch-intent storage and Alembic
migrations are implemented in US-004.1; see [Database migrations](database-migrations.md).

## Design patterns

The [design-pattern catalog](design-pattern/README.md) records each pattern's
purpose, application to ElasticScan, implementation status, tradeoffs, and
verification requirements.

- [Transactional Outbox](design-pattern/transactional-outbox.md): save jobs and
  publication intent atomically, then publish through a background dispatcher.
- [Idempotent Consumer](design-pattern/idempotent-consumer.md): handle duplicate
  queue deliveries using persisted job claims and terminal-state protection.
- [Publish–Subscribe](design-pattern/publish-subscribe.md): route SNS publications
  to scanner-specific SQS queues independently of worker instances.

The outbox and idempotent consumer currently describe proposed feature behavior.
Publish–subscribe infrastructure is configured locally, while application
publication and consumption remain pending. Documentation is not evidence that
the corresponding application logic is implemented.

See the [requirements](requirements.md) for the system workflow and acceptance
criteria, and [local development](local-development.md) for the verified bootstrap
configuration and startup instructions.
