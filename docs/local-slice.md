# Partial local integration slice (US-019.2)

This opt-in check joins authenticated submission, durable publication, queue
delivery, worker claims, outcome interpretation, persistence and retrieval. It
uses a simulated scanner and test orchestration. It does not activate a
production worker or demonstrate an actual authorized ZAP scan. US-019.2 remains
open; US-005 is unchanged.

## Prerequisites and command

Run from `elasticscan/` with the existing API image available and PostgreSQL,
LocalStack and Keycloak running and healthy:

```sh
python3 -B scripts/verify_local_slice.py
```

The helper starts a disposable API-image container with the source mounted
read-only and all four opt-in flags enabled: `RUN_LOCAL_SLICE_TESTS`,
`RUN_POSTGRES_TESTS`, `RUN_LOCALSTACK_TESTS` and `RUN_KEYCLOAK_TESTS`. It neither
builds images nor starts, repairs or replaces dependencies. The dedicated test
entry point exits unsuccessfully if any test fails, is skipped, or fewer/more
than six tests run. Normal unittest discovery skips this class unless every
flag is enabled. Existing `.env` credentials are read by Compose, never printed.

The tests require the default local PostgreSQL host, LocalStack endpoints,
Keycloak issuer/JWKS URL and `team_ids` claim. They obtain real tokens for
`alice`, `alice-peer` and `bob`. JWT signature, issuer, audience, lifetime and
trusted team resolution use the production dependencies. Token acquisition
creates development identity-provider sessions; these are not reset by cleanup.
An unavailable or broken Keycloak fails verification; it is not silently skipped
or recreated.

## Real and simulated components

| Component | Verification |
| --- | --- |
| HTTP submission and retrieval | Real production ASGI routes, invoked in process using existing request helpers; no HTTP server/socket or application lifespan startup |
| Identity and teams | Real Keycloak-issued tokens, actual JWKS verification and production membership resolution |
| Storage | Real PostgreSQL, existing Alembic migration applied to a unique disposable schema per test |
| Publication and routing | Production `publish_job`, real LocalStack SNS/SQS and existing provisioning code with unique resource names |
| Worker claim | Production `handle_message`, saved-message validation and committed execution claim |
| Scanner | Fixed `ScannerOutput` or injected `TimeoutError`; no process or target connection |
| Terminal outcome and acknowledgment | Test orchestration calls existing guarded persistence, reads the result and deletes that test delivery after commit |

The target policy receives controlled DNS answers for
`https://approved.example.invalid/`. It retains ordinary origin/public-address
validation. The returned public address is test data, never contacted. There is
no private-network exception or production target-policy configuration change.

## Six checks

1. Both teams submit and complete jobs with fixed findings. Owner and teammate
   reads work; cross-team reads match unknown-job 404 responses at pending,
   running and terminal stages. Ownership and UTC timestamps remain valid.
   Directly replaying the terminal message never invokes another adapter.
2. An injected scanner timeout produces a stored canonical safe failure without
   exposing the exception detail. This is not a real timeout/cleanup test.
3. Missing/malformed authentication, unauthorized team selection, existing
   invalid request examples and a disallowed loopback URL create no job/intent.
4. An unrelated routing attribute produces no queue delivery during a bounded
   observation interval; the production ZAP publication then reaches the queue
   and completes the simulated journey.
5. An injected publisher connection failure preserves pending state and dispatch
   intent. Moving the test retry time into the past permits real LocalStack
   publication and simulated completion. LocalStack is not stopped or restarted.
6. Malformed, unsupported-version and inconsistent deliveries leave the job
   pending without invoking the scanner boundary.

Each test registers cleanup for its schema, clients, topic, queue and dead-letter
queue, including ordinary setup/test failures. The engine is disposed before
dropping its schema. Default application schemas/topics/queues are not migrated,
purged or consumed. Abrupt process termination can still leave test resources;
verification should inspect resources with the `test_local_slice_` schema and
`test-local-slice-` messaging prefixes afterward.

## Patterns and acceptance boundaries

The tests reuse [Adapter](design-pattern/adapter.md) for the scanner interface
and [Dependency Injection](design-pattern/dependency-injection.md) for temporary
storage and controlled target resolution. This makes failures repeatable but
cannot prove physical scanner behavior. Existing transactional outbox and
repository functions are exercised without changing their implementation.

`process()` in `tests/integration/test_local_slice.py` is deliberately test-only
glue: the production worker handler currently discards returned scanner output
and the deployed worker remains idle. This check does not implement a production
poller, visibility/lease renewal, acknowledgment/recovery policy or cancellation.
The redelivery check calls the handler with the same body; it does not wait for
an SQS visibility expiry. API reads inspect each stage; there is no concurrent
external HTTP polling client.

This adds partial evidence for NFR-018/NFR-020/NFR-021 and AC-012/AC-022. Real ZAP
execution against an authorized isolated application, enforceable network
boundaries (US-005), execution-budget acceptance (US-017.1), correlated audit
logging (US-018.1), live process orchestration and AWS deployment remain pending.
No second scanner, new API schema, migration or dependency is introduced. A
successful run is not full acceptance of US-019.2 or the encompassing criteria.
See the [test guide](../tests/README.md#us-0192-partial-local-integration-slice)
for executed results and limitations.
