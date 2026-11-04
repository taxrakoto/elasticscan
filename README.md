# ElasticScan

ElasticScan is a planned security scanning service built with FastAPI. Clients submit scans through a REST API and retrieve their status and results. Amazon SNS and SQS distribute jobs to dedicated workers, and PostgreSQL stores job records and outcomes.

The MVP uses OWASP ZAP baseline scanning against authorized, allowlisted public URLs. API access uses OIDC-issued JWT access tokens, with jobs isolated by team. Local development will use Keycloak through Docker Compose.

## Current status

This repository currently contains the project documentation and an empty application scaffold. Python files contain no implementation. Dependency configuration, Dockerfiles, Docker Compose configuration, migration tooling, and AWS deployment resources have not been created yet. The application cannot be started at this stage.

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
| `docker/` | Container build and development configuration assets. A future Compose file will coordinate local services; none exists yet. |
| `docker/api/` | Build assets for the API container, such as its Dockerfile. |
| `docker/worker/` | Build assets for scanning workers and their scanner runtime dependencies. |
| `docker/keycloak/` | Reproducible local Keycloak configuration, such as realm/client setup and test-team configuration. Real secrets and runtime data must remain outside committed configuration. |
| `docs/` | Requirements and implementation planning. `requirements.md` contains the RAD; `user-stories.csv` contains stories, subtasks, recommended implementation order, and dependencies. |
| `infra/` | Infrastructure definitions and deployment configuration. |
| `infra/aws/` | Future AWS resources and policies for compute, messaging, persistence, networking, and access control. Infrastructure tooling has not yet been selected. |
| `migrations/` | Database migration configuration and execution support, to be added when migration tooling is configured. |
| `migrations/versions/` | Versioned schema changes applied in a controlled sequence. Editing a SQLAlchemy model alone will not update an existing database. |
| `scripts/` | Developer and operator command wrappers. Reusable application behavior belongs in `app/`. |
| `tests/` | Automated verification of application behavior. |
| `tests/conftest.py` | Shared pytest fixtures and test setup. Currently empty. |
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

## Placeholder and ignore files

- Empty `__init__.py` files mark the application directories as Python packages. They contain no startup logic.
- `.gitkeep` files allow otherwise empty directories to be tracked by Git. Remove a placeholder once its directory contains real tracked files.
- `.gitignore` excludes local secrets, virtual environments, Python/test caches, runtime data, generated reports, and infrastructure state. Example environment files are allowed, but must contain placeholders rather than credentials. Dependency lock files and database migrations should be committed when introduced.

## Where to begin

Follow `implementation_sequence` in `docs/user-stories.csv` for the recommended solo development order. Its first task is `US-019.1: Provide local service configuration`.

The `depends_on` column lists prerequisite subtask IDs. Independent tasks can run in parallel when their agreed contracts and fixtures are available. Parent stories group related outcomes and do not have their own implementation sequence.
