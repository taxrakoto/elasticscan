# Lifecycle retrieval contract (US-009.2)

`GET /scan/{job_id}` returns the current persisted job for the authenticated,
authorized team. Any member of that team may read it. The response is built
while the database session is open and always includes all twelve public
fields, including explicit JSON nulls. POST acceptance remains a three-field
snapshot and is unchanged.

## Public fields and states

| Field | Type and meaning |
| --- | --- |
| `job_id` | UUID identifying the saved job |
| `scan_type` | Saved scanner name, currently `zap` |
| `status` | `pending`, `running`, `completed`, `failed`, or proposed `cancelled` |
| `created_at`, `updated_at` | Required timezone-aware timestamps serialized in UTC |
| `started_at` | Nullable UTC execution start; required for running/completed, null for pending |
| `finished_at` | UTC terminal time; required exactly for completed/failed/cancelled |
| `result` | Exactly high/medium/low nonnegative integer counts for completed; otherwise null |
| `error` | Recognized safe code/message for failed; otherwise null |
| `cancellation_requested_by` | Nullable requesting subject, paired with request time |
| `cancellation_requested_at` | Nullable UTC request time, paired with requesting subject |
| `cancelled_at` | UTC cancellation completion time only for cancelled; equals finished_at |

| State | started_at | finished_at | result | error | Cancellation |
| --- | --- | --- | --- | --- | --- |
| pending | null | null | null | null | Request may be recorded |
| running | required | null | null | null | Request may be recorded |
| completed | required | required | counts | null | A racing request may remain recorded |
| failed | nullable | required | null | safe error | A racing request may remain recorded |
| cancelled | nullable | required | null | null | Requester, request and completion times required |

A failure before execution starts has a null start time. Cancellation requests
alone do not change the response status. Cancelled fixtures establish the proposed
representation, not cancellation execution; US-011.3 and US-012.3 must verify
that real outcome. Counts describe findings, so completed does not mean secure.
All six timestamp fields are normalized to UTC, regardless of database-session
or input offset. Naive timestamps are rejected.

The serializer exposes no team, creator, target parameters, worker ID, claim
token, lease, heartbeat, or attempt counter. The cancellation requester is an
explicit public field available only through the owning team's authorized read.

## Safe errors and inconsistent storage

`SafeFailure` reconstructs errors from `app/core/failure.py:safe_error` using only
the recognized code. Stored free-text messages and additional diagnostic fields
are discarded. The catalog includes scanner timeout, execution failure, invalid
output, invalid baseline execution, exhausted dispatch, and exhausted recovery.
Unknown codes, invalid counts or inconsistent lifecycle data produce the same
safe HTTP 503 as a database read failure. No raw validation details are returned.
The response validation does not repair stored rows or replace database guards.
Privileged/legacy writes may still produce incompatible rows requiring operator
repair. Timestamp ordering beyond the cancellation/finish equality is not checked.

## Examples

These are the version-one examples in `tests/retrieval_contract.py`, converted
from UTC+03:00 to UTC for JSON. The version describes fixtures and is not a new
public response field. Submission fixtures remain unchanged.

### pending

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "scan_type": "zap",
  "status": "pending",
  "created_at": "2026-11-12T09:00:00Z",
  "updated_at": "2026-11-12T09:00:00Z",
  "started_at": null,
  "finished_at": null,
  "result": null,
  "error": null,
  "cancellation_requested_by": null,
  "cancellation_requested_at": null,
  "cancelled_at": null
}
```

### running

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "scan_type": "zap",
  "status": "running",
  "created_at": "2026-11-12T09:00:00Z",
  "updated_at": "2026-11-12T09:00:00Z",
  "started_at": "2026-11-12T09:00:00Z",
  "finished_at": null,
  "result": null,
  "error": null,
  "cancellation_requested_by": null,
  "cancellation_requested_at": null,
  "cancelled_at": null
}
```

### completed

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "scan_type": "zap",
  "status": "completed",
  "created_at": "2026-11-12T09:00:00Z",
  "updated_at": "2026-11-12T09:00:00Z",
  "started_at": "2026-11-12T09:00:00Z",
  "finished_at": "2026-11-12T09:00:00Z",
  "result": {
    "high": 1,
    "medium": 2,
    "low": 3
  },
  "error": null,
  "cancellation_requested_by": null,
  "cancellation_requested_at": null,
  "cancelled_at": null
}
```

### failed

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "scan_type": "zap",
  "status": "failed",
  "created_at": "2026-11-12T09:00:00Z",
  "updated_at": "2026-11-12T09:00:00Z",
  "started_at": "2026-11-12T09:00:00Z",
  "finished_at": "2026-11-12T09:00:00Z",
  "result": null,
  "error": {
    "code": "scanner_timeout",
    "message": "Scanner execution exceeded its time limit."
  },
  "cancellation_requested_by": null,
  "cancellation_requested_at": null,
  "cancelled_at": null
}
```

### cancelled

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "scan_type": "zap",
  "status": "cancelled",
  "created_at": "2026-11-12T09:00:00Z",
  "updated_at": "2026-11-12T09:00:00Z",
  "started_at": null,
  "finished_at": "2026-11-12T09:00:00Z",
  "result": null,
  "error": null,
  "cancellation_requested_by": "alice",
  "cancellation_requested_at": "2026-11-12T09:00:00Z",
  "cancelled_at": "2026-11-12T09:00:00Z"
}
```

## HTTP outcomes

| Status | Meaning |
| --- | --- |
| 200 | Accessible saved job and valid response data |
| 401 | Missing or invalid bearer token |
| 403 | Missing, ambiguous or unauthorized team context |
| 404 | Unknown or cross-team UUID; identical `Job not found` body |
| 422 | Malformed job UUID |
| 503 | Database read failure or incompatible saved lifecycle data; `Job retrieval is temporarily unavailable` |

Retrieval performs no writes, DNS lookups, target-policy revalidation, publication
or scanner execution. Team filtering remains inside the repository query.

## Verification and acceptance boundaries

`tests/unit/test_retrieval_schema.py` checks the five examples, explicit nulls,
offset conversion, invalid states/counts, safe errors and pre-start failures.
`tests/integration/test_job_retrieval.py` uses the production route, signed test
identities, migrated disposable PostgreSQL schemas, guarded claims/completion/
failure writes, fresh database connections, two-team isolation and invalid legacy
storage. ZAP exit 0/1/2 examples pass through the actual normalizer. A classified
timeout uses an injected adapter failure, not a real scanner timeout.

Run focused checks from the application directory:

```sh
docker compose run --rm --no-deps -e RUN_POSTGRES_TESTS=1 -v "$PWD:/workspace:ro" -w /workspace --entrypoint python api -B -m unittest tests.unit.test_retrieval_schema tests.integration.test_job_retrieval -v
```

Run `python3 scripts/verify_result_summaries.py` for isolated real ZAP scans,
normalized-count persistence and HTTP retrieval of those exact counts. This
replaces application database connections, not a database process restart.
No deployed application containers are replaced by these checks.

The story supports FR-005/006/007, AC-002's retrieval checks and AC-004's persisted
outcome response. Live queue-to-worker integration, complete polling acceptance
(US-009.3), cancellation execution, and full egress enforcement remain separate.
See [Safe failures](safe-failures.md), [Atomic result summaries](result-summaries.md)
and [Repository](design-pattern/repository.md).
