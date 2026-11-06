# ElasticScan architecture

ElasticScan separates the HTTP API, PostgreSQL persistence, message dispatch,
and scanner execution into independent responsibilities. SNS routes messages to
scanner-specific SQS queues; workers execute scans and persist their outcomes.

The current implementation is a local bootstrap: the API exposes liveness and
OpenAPI, PostgreSQL and messaging infrastructure are provisioned, and Keycloak
provides development identities for two teams. Dispatcher and worker processes
remain idle placeholders. SQLAlchemy job and dispatch-intent storage and Alembic
migrations are implemented in US-004.1; see [Database migrations](database-migrations.md).
US-001.1 implements trusted JWT validation and a reusable FastAPI authentication
dependency. US-002.1 adds a team-context dependency: bearer token → verified
claims → authorized `TeamContext` → future job handler. It validates configured
membership claims and an optional `X-Team-ID` selector; multi-team callers must
select a member team. See [Authentication](authentication.md) for the contract.
US-002.2 connects the context to `app/repositories/jobs.py`: the repository inserts
ownership from `TeamContext`, while the existing database trigger rejects later
ownership changes through object edits, bulk updates, or raw SQL. The caller
owns the transaction; the function flushes the insert without committing.
US-004.2 adds `app/services/acceptance.py`: validated submission and authorized
context → acceptance service → one job/dispatch-intent transaction → immutable
`AcceptedScan` result. The service owns its session and returns only after commit;
errors roll back the transaction and propagate to the caller. It makes no SNS
or scanner calls. Job routes and team-scoped reads remain pending.

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
- [Dependency Injection](design-pattern/dependency-injection.md): supply verified
  identity and claim-mapping settings to the team authorization dependency.
- [Value Object](design-pattern/value-object.md): carry the resolved team and
  current caller together in an immutable `TeamContext`.
- [Repository](design-pattern/repository.md): insert validated jobs with explicit
  ownership mapping, leaving transaction orchestration to the caller.

The outbox's atomic database acceptance is implemented and covered by PostgreSQL
rollback and visibility tests. Its publisher, retry loop, and the idempotent
consumer remain proposed feature behavior. Publish–subscribe infrastructure is
configured locally, while application publication and consumption remain pending.
Documentation is not evidence that pending behavior is implemented.

See the [requirements](requirements.md) for the system workflow and acceptance
criteria, and [local development](local-development.md) for the verified bootstrap
configuration and startup instructions.
