# Local development (US-019.1)

This is the bootstrap portion of Section 3.2 and AC-022 in the RAD. It provides
PostgreSQL, FastAPI, Keycloak, SNS/SQS emulation, a dispatcher with bounded
publication recovery, and an idle ZAP worker with the pinned scanner runtime. Production submission
and team-scoped retrieval are implemented; scanner adapters remain future work.
US-004.1 adds job/dispatch-intent tables and Alembic migrations; apply them explicitly as described in the
[database migration guide](database-migrations.md). The worker explicitly logs
its idle status and does not execute jobs.
Dispatcher and worker health checks indicate process startup only. Apply
migrations before enabling publication; see the dispatcher section below.

## Start and verify

Requirements: Docker engine with Compose v2.20+ and Python 3.9+ on the host. Allow
approximately 4 GB of free memory for the stack and additional disk space for the
ZAP image. Run these commands from `elasticscan/`:

```sh
python3 scripts/setup_local.py
docker compose up --build -d --wait --wait-timeout 240
docker compose ps
docker compose exec -T api python -m scripts.verify_local
curl --fail http://localhost:8000/health/live
```

Setup generates `.env` with random development passwords and mode 0600. It refuses
to overwrite an existing file. `.env` is ignored by Git and excluded from Docker
builds. `.env.example` documents every setting without embedding passwords.
The `realm-init` one-shot service injects the runtime fixture password into a
Docker volume before Keycloak imports the realm. That volume contains credentials;
only Keycloak mounts it after initialization. No host Docker socket is mounted.
Do not share the generated `.env`, realm volume, or tokens.

Published ports bind to loopback: API 8000, PostgreSQL 5432, Keycloak 8080, and
LocalStack 4566. Ensure they are free before starting. PostgreSQL persists in the
`postgres-data` volume. Keycloak's development database is deliberately ephemeral;
container recreation imports the same client and fixture identities with fresh
signing keys. Restart API validators or refresh their key caches after recreation.

`verify_local.py` checks a real PostgreSQL connection, API liveness, cryptographic
JWT validation through the application validator against the configured JWKS,
audience/issuer rejection, three users' team claims, queue source policy and retry configuration, and delivery of
`zap` messages while filtering another scan type. It deletes only its own marker
messages. Run it against this bootstrap stack before implementing consumers;
a future active worker could consume its messages. It refuses real AWS endpoints.
It does not scan any target.

## Local messaging strategy

The proposed local default is **LocalStack 4.0.3**, running only SNS and SQS. This
fixed emulator version is intentional and does not require an AWS account or
production credentials. It is a development choice pending technical-default
review, not production AWS acceptance. Runtime images use fixed release tags;
Python dependencies are fully pinned with hashes in `requirements.lock`.
To intentionally update the lock after editing `requirements.in`, use the pinned
Python/uv tooling image, then rebuild and rerun verification:

```sh
docker build -f docker/tooling/Dockerfile -t elasticscan-tooling:local .
docker run --rm -v "$PWD:/work" elasticscan-tooling:local pip compile --python-version 3.12 --generate-hashes --output-file requirements.lock requirements.in
```

Image tags are not digest locks; review upgrades and resolve image digests before
production rollout. LocalStack behavior does not replace AWS integration tests.

The ready hook idempotently provisions:

- `elasticscan-jobs` SNS topic;
- `elasticscan-zap` SQS queue with 60-second visibility and a three-receive DLQ policy;
- `elasticscan-zap-dlq` dead-letter queue;
- a raw-delivery SNS subscription filtered on message attribute `scan_type=zap`;
- an SQS policy granting `sns.amazonaws.com` SendMessage only from that topic ARN.

The LocalStack health check passes only after provisioning succeeds. The
visibility/retry values are bootstrap defaults, not approved scanner lease and
recovery behavior. Later worker stories must implement visibility renewal and
bounded recovery before scans run.

Container defaults:

| Setting | Value |
| --- | --- |
| `AWS_DEFAULT_REGION` | `us-east-1` (local placeholder, not an AWS deployment decision) |
| `SNS_ENDPOINT_URL`, `SQS_ENDPOINT_URL` | `http://localstack:4566` |
| `SNS_TOPIC_ARN` | `arn:aws:sns:us-east-1:000000000000:elasticscan-jobs` |
| `SQS_QUEUE_URL` | `http://localstack:4566/queue/us-east-1/000000000000/elasticscan-zap` |
| `PGHOST`, `PGPORT` | `postgres`, `5432` |
| `PGDATABASE`, `PGUSER`, `PGPASSWORD` | Derived from `POSTGRES_*` in `.env` |

For host tools, use `http://localhost:4566` as the endpoint and replace `localstack`
with `localhost` in the queue URL. Host PostgreSQL connects at `localhost:5432`.
The emulator does not persist state; restart provisions these named resources
again. Queued development messages may be lost on recreation.

Service environment settings support alternative SNS/SQS endpoints and resource
identifiers. To use real AWS in a future integration run, provision topic, queues,
subscription, filtering, source policy, and IAM separately; replace both endpoint
URLs with regional AWS endpoints, ARNs and queue URLs with actual resources, and
inject restricted AWS credentials/session token. Do not use dummy `test`
credentials for AWS. The default Compose dependency graph still starts the
emulator; a dedicated AWS override belongs with deployment configuration. Nothing
in this bootstrap provisions billable resources or verifies production IAM.
If changing the local region, change the ARN and queue URL in `.env` consistently.

## OIDC and trusted team mapping

For the separate initial scan-target configuration, see
[Initial target policy](target-policy.md). `TARGET_ALLOWED_ORIGINS` defaults to
`[]` when absent, so existing `.env` files grant no target access. To authorize an
actual public target, obtain its owner's permission and set a JSON array containing
one exact HTTP/HTTPS origin in `.env`, then recreate the API so it receives the new
environment (rebuild if source changed). Example syntax only:

```dotenv
TARGET_ALLOWED_ORIGINS=["https://approved.example.invalid"]
```

Wildcard/subdomain inheritance is not supported. Adding a private fixture IP to
the allowlist does not bypass the public-address checks. No private-network test
exception exists yet; it belongs to US-005.3. Production submission uses the
initial checks since US-003.2. The US-005.2 application phase also rechecks the
target in the reusable worker handoff and generates an exact-origin ZAP scope.
Multiple configured origins are now rejected; all paths in the single origin
remain allowed. Worker callers must receive the same updated environment.
Real ZAP crawling and AWS network enforcement remain pending. See
[Single fixed scanner origin](scanner-scope.md) for configuration and limits.

The reusable [ZAP baseline adapter](zap-baseline.md) runs in the narrowed worker
image. Its provisional `ZAP_CRAWL_MINUTES=1` and
`ZAP_TOTAL_TIMEOUT_SECONDS=900` defaults are independent. Build with
`docker compose build zap-worker`; this does not replace the running idle worker.
Use `sh scripts/verify_zap_baseline.sh` for isolated real-scanner tests, not a
public website. Full SQS consumption, lease renewal during execution, normalized
outcomes and durable completion remain later integrations. The image copies only
the worker/scanner modules and their shared dependencies, not API routes or the
dispatcher. The private `.env` is not edited by this task.

The committed `docker/keycloak/realm.json` defines realm `elasticscan` and public
client `elasticscan-dev`. Registration is disabled. Development-only password
grants are enabled for command-line fixture token acquisition. Production clients
must choose an appropriate authorization-code or service-account flow separately.

| Fixture username | Signed `team_ids` claim | Purpose |
| --- | --- | --- |
| `alice` | `["team-alpha"]` | First team |
| `alice-peer` | `["team-alpha"]` | Another member of the same team |
| `bob` | `["team-beta"]` | Second team |

Only administrator-provisioned user attributes supply membership. The mapper
copies the multi-valued `team_ids` attribute into the signed access token. Do not
expose this attribute for user editing or trust request fields to assign teams.
Future API authorization must validate this token before interpreting the claim;
a team selector cannot grant membership.

```sh
# Prints a short-lived bearer token; keep it out of logs and committed files.
python3 scripts/get_dev_token.py alice
python3 scripts/get_dev_token.py bob
```

The shared fixture password and admin password are generated in `.env`. The admin
console is `http://localhost:8080/admin/`. Access tokens expire after five minutes.

| Validation setting | Value |
| --- | --- |
| `OIDC_ISSUER` | `http://localhost:8080/realms/elasticscan` |
| `OIDC_AUDIENCE` | `elasticscan-api` |
| `OIDC_ALGORITHMS` | `RS256` explicit allowlist |
| `OIDC_TEAM_CLAIM` | `team_ids` |
| Container `OIDC_JWKS_URL` | `http://keycloak:8080/realms/elasticscan/protocol/openid-connect/certs` |
| Host discovery | `http://localhost:8080/realms/elasticscan/.well-known/openid-configuration` |
| Host JWKS | `http://localhost:8080/realms/elasticscan/protocol/openid-connect/certs` |

Keycloak's fixed public hostname makes tokens obtained through either host or
container URLs carry the **same localhost issuer**. Container validators fetch
keys through the explicit internal JWKS URL while still requiring that public
issuer string; `localhost` inside the API container is not Keycloak. Host
validators use the host JWKS URL. Never derive an accepted issuer or key URL from
untrusted token headers. HTTP and password grants are confined to this local
development configuration.

## Component handoff and acceptance boundaries

US-004.1 provides SQLAlchemy models and versioned migrations. Run the commands in
[Database migrations](database-migrations.md) to create or update the schema;
a healthy PostgreSQL instance alone does not imply the schema exists. US-001.1 provides
trusted token validation and the `require_identity` dependency using the OIDC
settings above; see [Authentication](authentication.md) for behavior and tests.
Production scan submission and retrieval require JWT validation and team-scoped
authorization. US-006.1 replaces the idle dispatcher command with automatic
publication; US-006.3 adds bounded retry and expired-claim recovery. US-007 will
replace the idle worker command with actual execution. The worker
image already contains `zap-baseline.py`, with CPU and memory limits in Compose.
No scan execution or private-network target exception is enabled here.

For the eventual scanner extension point (AC-012), routes and client schemas stay
independent of scanner commands; `app/scanners/` will hold adapters,
`app/workers/` will coordinate execution, and `app/messaging/` will hold transport.
A future scan type needs a versioned payload contract, adapter, dedicated worker
queue, matching SNS attribute filter, and compatibility tests. This bootstrap
uses only `scan_type=zap`; adapter contracts and extension tests belong to
US-003.1 and US-019.3. Full AC-012/AC-022 acceptance, authenticated submit/poll,
migration deployment, two-team isolation, and authorized baseline scans remain gates in
US-019.2/US-019.3 and AWS deployment stories.

## Stop, reset, and troubleshoot

```sh
docker compose logs --tail=100 api dispatcher zap-worker localstack keycloak
docker compose stop       # retains containers and PostgreSQL data
docker compose start --wait
docker compose down       # removes this stack's containers, retains volumes
# Destructive: removes this stack's PostgreSQL data and generated realm secrets.
docker compose down -v
```

After editing realm configuration or rotating `.env` fixture passwords, recreate
Keycloak and regenerate its import: `docker compose up -d --force-recreate
realm-init keycloak`, then wait for Keycloak health. Its ephemeral database avoids
Keycloak's skip-existing-realm import behavior on recreation. PostgreSQL password
changes require changing the database role as well or explicitly resetting the
local database volume; container environment variables do not rotate existing
roles. Keep production data out of this stack.

A failed LocalStack health check usually indicates a ready-hook error; inspect
its logs. Do not bypass the check. For invalid tokens, check issuer, audience,
expiry and the correct host/container JWKS URL before changing validation rules.
Rebuild after editing Python sources (`docker compose up -d --build --wait`).

Implementation references: [Keycloak container import](https://www.keycloak.org/server/containers),
[Keycloak hostname behavior](https://www.keycloak.org/server/hostname), and
[LocalStack initialization hooks](https://blog.localstack.cloud/how-to-automatically-load-state-in-localstack/).

## Recorded bootstrap verification

Verified on 2026-11-04 with Docker Engine 29.8.2, Compose 5.5.0, OrbStack Linux
ARM64, container Python 3.12.8, and host Python 3.9.6.

- Compose configuration, Python syntax, and shell syntax checks passed.
- Both application images built with the hashed dependency lock; all six
  long-running services became healthy and realm initialization exited with 0.
- PostgreSQL connectivity, API liveness, and OpenAPI generation passed.
- Keycloak issued signed RS256 tokens for both teams and a second alpha member;
  JWKS signature verification and issuer/audience checks passed. Corrupted
  signatures and incorrect issuer/audience validation settings were rejected.
- The host token helper obtained tokens for both teams with the same issuer as
  container clients. Host API access passed. No bearer tokens were logged.
- Provisioning ran twice without duplicate subscriptions. The queue source policy,
  DLQ retry configuration, raw delivery and `scan_type=zap` filter passed checks;
  controlled ZAP messages arrived and unrelated scan-type messages did not.
- LocalStack recreation and restart completed provisioning successfully; the
  final smoke test passed again. Its health check reads the current boot's hook
  completion state, so a stale filesystem marker cannot bypass initialization.

US-019.1 is complete at its stated bootstrap scope. This evidence does not close
full AC-012 or AC-022, nor claim a running scan workflow or API team isolation.


## US-006.2: dedicated queue routing

The local provisioning function explicitly configures `FilterPolicyScope` as
`MessageAttributes`, `FilterPolicy` as `{"scan_type":["zap"]}`, and raw delivery.
It reapplies subscription attributes when rerun, restoring changed settings
without creating a second subscription. The queue policy permits SNS SendMessage
from the configured jobs topic ARN; worker read/delete permissions are separate.
Raw delivery puts the original publisher body in SQS without an SNS JSON envelope.

After updating the mounted provisioning source, apply it to the running emulator:

```sh
docker compose exec -T localstack python3 /opt/bootstrap/provision.py
```

This reapplies the subscription configuration and queue source policy without
recreating LocalStack or purging its queues. Existing messages are not rewritten.
The subscription filters the `scan_type` message attribute, not the similarly
named body field. The future application publisher must send that attribute.

Run isolated routing verification from `elasticscan/`:

```sh
docker compose run --rm --no-deps -e RUN_LOCALSTACK_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.integration.test_queue_routing -v
```

Tests create uniquely named topics and queues and remove them through registered
cleanup operations, including on assertion failure. They never consume or purge
application queues. Checks inspect source policy, subscription and DLQ settings;
restore deliberately changed subscription attributes; and publish controlled
messages proving attribute filtering and raw delivery. A missing attribute and an
unrelated attribute are rejected even when the body says zap. A zap attribute is
accepted even when the diagnostic body says otherwise; these are routing fixtures,
not valid application jobs. The nondelivery observation window is 15 seconds.
Matching duplicates are allowed. Without the flag, tests skip explicitly.

These tests accept only the default container LocalStack endpoints and use dummy
credentials. They establish local routing configuration, not real AWS IAM
enforcement, application dispatch, outage recovery, or worker execution. FR-016
and AC-003 are covered locally; US-006.3 adds separate FR-014/AC-007
publication-recovery verification. See the [test guide](../tests/README.md) and
[Publish–Subscribe pattern](design-pattern/publish-subscribe.md).

For a later real AWS deployment, Terraform should create equivalent topics,
queues, subscription attributes, and source policy. Keep the Python provisioning
script for local development. Application clients should use configured endpoints,
ARNs, queue URLs, and appropriate AWS IAM credentials; the messaging interface need
not change merely because the infrastructure provider changes. AWS integration
verification and filter-policy propagation are separate from local acceptance.


## US-006.1: dispatcher process

The API and dispatcher share an image but run as separate containers. The API
accepts and saves jobs; `python -m app.dispatchers.main` reads committed intents
and publishes versioned messages to SNS. No public dispatcher port is required.
The existing environment supplies PostgreSQL settings, `SNS_TOPIC_ARN`,
`SNS_ENDPOINT_URL`, and AWS credentials/region. LocalStack uses dummy credentials.
Deploy database migrations before starting the dispatcher.

From `elasticscan/`, with local dependencies healthy and migrations applied:

```sh
docker compose build api
docker compose up -d --no-deps dispatcher
docker compose logs --tail 50 dispatcher
```

This rebuilds the shared image and replaces/starts the dispatcher, while leaving
an already running API container in place. Starting it will publish eligible
application jobs, including due retries and expired claims within their budget.
Use `docker compose stop dispatcher` to stop it.
The restart policy is `unless-stopped`. Existing containers do not receive source
changes until rebuilt/recreated; running tests with a source bind mount does not
deploy them. Implementation verification uses an isolated subprocess/schema/topic
and does not activate this process against application jobs.

Polling selects at most ten due attempts per pass and waits two seconds.
Connection/read timeouts are 3/5 seconds, with one SDK attempt. SIGTERM/SIGINT stop
new work and let the active call finish. `/tmp/dispatcher-ready` indicates process
startup/liveness only; it does not report PostgreSQL/SNS readiness or progress.
Logs include job IDs, outcomes, attempt counts, retry times and safe error codes
without targets or credentials.

US-006.3 limits publication to five total committed attempts. Failures retry after
jittered exponential delays of 1–2, 2–4, 4–8 and 8–16 seconds, plus polling latency.
Interrupted claims become eligible after the 60-second lease plus saved backoff.
Invalid/exhausted intent stays stored and requires operator action; a restart
does not reset the budget. A final interrupted attempt is identifiable from its
counter and expired lease. Use the publication guide's read-only SQL and preserve
diagnostics before planning targeted recovery. No automatic reset or external
alerting is provided. No worker/scanner execution is added.
See [Job publication](job-publication.md) for the contract, operations, duplicate
risk, limitations, and [tests](../tests/README.md#us-0061-versioned-publication-and-dispatcher).
