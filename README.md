# ElasticScan

ElasticScan is a planned security scanning service built with FastAPI. Clients submit scans through a REST API and retrieve their status and results. Amazon SNS and SQS distribute jobs to dedicated workers, and PostgreSQL stores job records and outcomes.

The MVP uses OWASP ZAP baseline scanning against authorized, allowlisted public URLs. API access uses OIDC-issued JWT access tokens, with jobs isolated by team. Local development will use Keycloak through Docker Compose.

## Current status

The local bootstrap stack is implemented: API liveness, PostgreSQL, Keycloak with
two team fixtures, LocalStack SNS/SQS provisioning, a dispatcher with bounded publication recovery,
and an idle ZAP worker process. US-004.1 adds SQLAlchemy job/dispatch-intent
storage and versioned Alembic migrations. US-001.1 adds trusted JWT validation and a reusable
FastAPI bearer-authentication dependency. US-002.1 adds authorized team-context
resolution from verified membership claims, including explicit selection for
multi-team callers. US-002.2 adds pending-job insertion using that verified
context, with immutable ownership enforced by the existing database trigger.
US-004.2 adds a reusable acceptance service that commits the job and dispatch
intent together before returning. US-005.1 adds reusable initial-target validation
with a default-deny exact-origin allowlist and IPv4/IPv6 checks. US-003.2 connects
these components to production `POST /scan`: an authenticated, authorized, valid
request receives HTTP 202 with `job_id`, `scan_type: "zap"`, and `status: "pending"`
only after the job and dispatch intent commit. This status is an acceptance
snapshot, independent of later worker progress. Persistence errors return a safe
HTTP 503: durable acceptance could not be confirmed. Connection loss during commit
can leave its outcome uncertain; automatic retries are not implemented and a
repeated request can create another job. US-009.1 adds authenticated
`GET /scan/{job_id}` returning job identity and current stored state for the
selected authorized team. US-009.2 adds UTC timestamps, nullable severity counts,
safe errors and cancellation metadata. See the
[Retrieval contract](docs/retrieval-contract.md). Any owning-team member can read the
job. Unknown and cross-team jobs return identical HTTP 404 responses; malformed
UUIDs return HTTP 422. Live queue-to-worker integration, cancellation execution,
and complete crawl-time target controls remain feature work.
US-008.1 adds reusable guarded lifecycle functions: pending-to-running,
worker-owned running-to-completed/failed, and pending failure for exhausted
service processing. Conditional PostgreSQL updates protect terminal outcomes
through these operations. The deployed worker process remains idle. Reusable
execution, renewable claims, validated result persistence and safe failures exist
in subsequent stories; live orchestration, bounded recovery and cancellation
execution remain pending. See
[Guarded job lifecycle](docs/job-lifecycle.md).

See [Local development](docs/local-development.md) for credentials, configuration,
token acquisition, verification, and acceptance boundaries. See
[Database migrations](docs/database-migrations.md) for applying and evolving the schema,
and [Authentication](docs/authentication.md) for trust settings and verification.
See [Initial target policy](docs/target-policy.md) for approved origins, address
checks, and the limits of initial validation. See the
[Submission contract](docs/submission-contract.md) for request examples, bearer
authentication, team selection, acceptance responses, and errors.

```sh
python3 scripts/setup_local.py
docker compose up --build -d --wait --wait-timeout 240
docker compose exec -T api python -m scripts.verify_local
```

## Project structure

```text
elasticscan/
├── app/
│   ├── main.py
│   ├── api/
│   │   └── routes/
│   ├── core/
│   ├── db/
│   ├── models/
│   ├── schemas/
│   ├── repositories/
│   ├── services/
│   ├── messaging/
│   ├── dispatchers/
│   ├── scanners/
│   ├── workers/
│   └── maintenance/
├── docker/
│   ├── api/
│   ├── worker/
│   └── keycloak/
├── docs/
├── infra/
│   └── aws/
├── migrations/
│   └── versions/
├── scripts/
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
└── .gitignore
```

The responsibilities below describe the intended implementation.

## Application folders

| Path | Purpose | Examples of future contents |
| --- | --- | --- |
| `app/` | Python package containing the API and background processing components. Different processes can use different parts of this package. | Application modules and package initializers. |
| `app/main.py` | FastAPI application entry point. Assembles the API and its startup/shutdown behavior. | Application instance, router registration, and lifecycle setup. |
| `app/api/` | HTTP interface and shared API dependencies. Handles authentication context, request handling, and HTTP responses. | Dependencies that resolve the current caller, team, and database session. |
| `app/api/routes/` | Endpoint definitions. Routes validate input through schemas, call services, and return the documented response. | Submit, retrieve, list, cancel, and delete scan endpoints. |
| `app/core/` | Shared application configuration and security primitives. | Environment settings, JWT validation, trusted issuer configuration, and logging setup. |
| `app/db/` | Database connection and session infrastructure. | SQLAlchemy engine, session factory, and shared model base. |
| `app/models/` | SQLAlchemy models describing persisted entities and database relationships. | Scan jobs, team ownership fields, dispatch records, and deletion tombstones. |
| `app/schemas/` | Pydantic models for validating and serializing external data. These are distinct from database models. | Scan requests, status responses, result summaries, and versioned message payloads. |
| `app/repositories/` | Database queries and persistence operations. Provides explicit team-scoped reads and guarded writes. | Job lookup, filtered listings, atomic worker claims, and outbox queries. |
| `app/services/` | Business workflows and policy coordination. Connects validation, authorization, persistence, and job-management rules. | Scan submission, cancellation, and terminal-job deletion workflows. |
| `app/messaging/` | SNS/SQS integration and transport operations. | SNS publication, SQS polling, visibility extension, message deletion, and payload serialization. |
| `app/dispatchers/` | Background processing that publishes durable dispatch intent to SNS and tracks publication attempts. | Outbox polling, publication retries, and delivery diagnostics. |
| `app/scanners/` | Scanner adapters that invoke tools and normalize their output. Keeps tool-specific execution separate from queue handling. | Shared adapter contract, ZAP baseline invocation, and severity-result parsing. |
| `app/workers/` | Queue-consuming processes that claim jobs, invoke scanner adapters, and persist outcomes. | ZAP worker loop, lease renewal, cancellation handling, and acknowledgment decisions. |
| `app/maintenance/` | Recurring housekeeping and recovery operations. | Retention cleanup, abandoned-job reconciliation, and dead-letter recovery coordination. |

Keep routes focused on HTTP behavior, services on workflows, and repositories on database operations. Workers coordinate execution, scanner adapters operate scanning tools, and messaging modules handle transport.

## Supporting folders

| Path | Purpose |
| --- | --- |
| `docker/` | Container build and development configuration assets. The root `compose.yaml` coordinates local infrastructure and bootstrap services. |
| `docker/api/` | Build assets for the API container, such as its Dockerfile. |
| `docker/worker/` | Build assets for scanning workers and their scanner runtime dependencies. |
| `docker/keycloak/` | Reproducible local Keycloak configuration, such as realm/client setup and test-team configuration. Real secrets and runtime data must remain outside committed configuration. |
| `docs/` | Requirements and implementation planning. `requirements.md` contains the RAD; `../user-stories.csv` contains stories, subtasks, recommended implementation order, and dependencies. |
| `infra/` | Infrastructure definitions and deployment configuration. |
| `infra/aws/` | Future AWS resources and policies for compute, messaging, persistence, networking, and access control. Infrastructure tooling has not yet been selected. |
| `migrations/` | Alembic configuration and execution support; see the database migration guide. |
| `migrations/versions/` | Versioned schema changes applied in a controlled sequence. Editing a SQLAlchemy model alone will not update an existing database. |
| `scripts/` | Developer and operator command wrappers. Reusable application behavior belongs in `app/`. |
| `tests/` | Automated verification of application behavior. |
| `tests/conftest.py` | Shared pytest fixtures and test setup. Shared fixtures will be introduced with feature tests. |
| `tests/unit/` | Focused tests of individual rules or components using controlled dependencies. |
| `tests/integration/` | Tests of component boundaries, such as repositories with PostgreSQL or messaging with configured SNS/SQS endpoints. |
| `tests/e2e/` | Tests of complete user flows, including authenticated submission, asynchronous processing, result retrieval, and team isolation. |
| `tests/fixtures/` | Reusable test data, sample messages, scanner outputs, and authorized target fixtures. No real credentials or sensitive scan results. |

## How the components fit together

The intended scan flow is:

1. A client obtains a JWT access token from the configured identity provider. Locally, this will be Keycloak.
2. An API route validates the request and resolves the caller's authorized team context.
3. A service checks target policy and creates the job plus durable dispatch intent through the persistence layer.
4. The API returns HTTP 202 without waiting for the scan.
5. A dispatcher publishes the job through SNS to the appropriate SQS queue.
6. A worker claims the job, invokes the ZAP scanner adapter, and persists the outcome.
7. The client retrieves the team's job through the API.

The API process does not execute scans directly. Workers and dispatchers are intended to run independently of the API, even though they share this Python package.

See [Architecture](docs/architecture.md) for the transactional outbox pattern,
dispatcher retries, and duplicate-delivery handling.
The [design-pattern catalog](docs/design-pattern/README.md) contains the individual
pattern documents and their implementation status.

## Placeholder and ignore files

- Empty `__init__.py` files mark the application directories as Python packages. They contain no startup logic; `main.py` and `bootstrap.py` provide the current service entry points.
- `.gitkeep` files allow otherwise empty directories to be tracked by Git. Remove a placeholder once its directory contains real tracked files.
- `.gitignore` excludes local secrets, virtual environments, Python/test caches, runtime data, generated reports, and infrastructure state. Example environment files are allowed, but must contain placeholders rather than credentials. `requirements.lock` is committed; versioned database migrations are committed alongside their models.

## Where to begin

Follow `implementation_sequence` in `../user-stories.csv` for the recommended solo development order. `US-019.1` provides the bootstrap described in the local development guide; full scan-flow verification belongs to `US-019.2`.

The `depends_on` column lists prerequisite subtask IDs. Independent tasks can run in parallel when their agreed contracts and fixtures are available. Parent stories group related outcomes and do not have their own implementation sequence.


## Versioned job publication (US-006.1 and US-006.3)

The separate dispatcher container discovers committed, eligible jobs and publishes
version-one messages to SNS with the matching `scan_type` attribute. Short claims
and conditional finalization record outcomes without holding locks during SNS
calls. US-006.3 adds up to five total attempts, jittered backoff and expired-claim
recovery. Invalid/exhausted intent remains available for operator inspection;
prolonged outages require intervention and uncertain outcomes can cause duplicate
publication. The worker remains idle. See [Job publication](docs/job-publication.md)
and [dispatcher operation](docs/local-development.md#us-0061-dispatcher-process).
