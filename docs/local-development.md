# Local development (US-019.1)

This is the bootstrap portion of Section 3.2 and AC-022 in the RAD. It provides
PostgreSQL, a FastAPI liveness endpoint, Keycloak, SNS/SQS emulation, an idle
dispatcher, and an idle ZAP worker with the pinned scanner runtime. No scan routes,
job tables, migrations, dispatch loop, or scanner adapter exist yet. The idle
processes explicitly log their status and do not consume or execute jobs.
Their health checks indicate process startup only, not readiness to accept scans.

## Start and verify

Requirements: Docker engine with Compose v2.20+ and Python 3.9+ on the host. Allow
approximately 4 GB of free memory for the stack and additional disk space for the
ZAP image. Run these commands from `elasticscan/`:

```sh
python3 scripts/setup_local.py
docker compose up --build -d --wait --wait-timeout 240
docker compose ps
docker compose exec -T api python scripts/verify_local.py
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
JWT validation against the configured JWKS, audience/issuer rejection, three
users' team claims, queue source policy and retry configuration, and delivery of
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

US-004.1 adds SQLAlchemy models and versioned migrations. No migrations can be run
yet; a healthy PostgreSQL instance does not imply the schema exists. US-001.1 adds
API token validation using the OIDC settings above. All future scan operations
must require JWT validation and team-scoped authorization. This task exposes only
a public liveness endpoint, and does not demonstrate API-level team isolation.
US-006 and US-007 replace the idle dispatcher and worker commands with actual
entry points while preserving the separate images and configuration. The worker
image already contains `zap-baseline.py`, with CPU and memory limits in Compose.
No scan execution or private-network target exception is enabled here.

For the eventual scanner extension point (AC-012), routes and client schemas stay
independent of scanner commands; `app/scanners/` will hold adapters,
`app/workers/` will coordinate execution, and `app/messaging/` will hold transport.
A future scan type needs a versioned payload contract, adapter, dedicated worker
queue, matching SNS attribute filter, and compatibility tests. This bootstrap
uses only `scan_type=zap`; adapter contracts and extension tests belong to
US-003.1 and US-019.3. Full AC-012/AC-022 acceptance, authenticated submit/poll,
migrations, two-team isolation, and authorized baseline scans remain gates in
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
