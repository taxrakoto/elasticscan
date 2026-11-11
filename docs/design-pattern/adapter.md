# Adapter

**Status:** US-007.1 implements a reusable worker handoff boundary and tests it
with a recording adapter. Concrete ZAP execution is pending US-007.2/US-007.3.
The deployed worker remains an idle bootstrap process.

## Problem and components

Message validation and database claims should not depend on scanner commands.
`app/scanners/base.py` defines `ScannerAdapter.run(request) -> None` and the
frozen `ScanRequest(job_id, url)` value object. A Protocol describes the method
an implementation must provide without requiring inheritance. The caller injects
both the adapter and session factory into `app/workers/zap.py:handle_message`.
This reuses [Dependency Injection](dependency-injection.md) and the existing
[Repository](repository.md) lifecycle functions. The contract is the Adapter
extension point; adapting ZAP's actual process interface remains later work.

## Interaction and decisions

1. Parse the raw JSON body and require integer schema version 1 (not a Boolean,
   decimal or string). Validate all fields with the shared `JobMessage` schema.
2. Load the saved job by UUID. Compare scan type and the entire parameter object
   with the message's JSON-serialized typed parameters. Accepted jobs store that
   same normalized representation; differently serialized saved URLs are rejected.
3. Call the atomic pending-to-running guard, excluding cancellation requests,
   competing starts and nonpending jobs. Commit the transaction.
4. Only after successful commit, call the adapter with the saved job ID and
   validated URL. No untrusted command or ownership field is forwarded.

| Decision | Meaning |
| --- | --- |
| `invalid_message` | JSON/schema/version rejected before storage access |
| `missing_job` | No saved job; never reconstruct one from the message |
| `inconsistent_message` | Saved scan type or complete parameters disagree |
| `not_started` | Atomic start guard did not match |
| `invoked` | Adapter returned; this is not a completed scan outcome |

These decisions are not queue acknowledgment instructions. The function does not
receive an SQS receipt handle, poll a queue, delete messages, or persist terminal
results. Database and adapter exceptions propagate. A precommit failure prevents
invocation; connection loss during commit can leave the saved start uncertain.
An adapter failure or process crash after commit leaves the job running, with no
automatic retry. Renewable leases and recovery must precede running-job reclamation.
Pending cancellation metadata blocks starts, but running cancellation and deletion
tombstones are not implemented here. Concurrent deletion/reclamation is outside
this foundation. No exactly-once physical execution guarantee is claimed.

## Benefits, tradeoffs and acceptance

The small contract separates scanner-specific behavior from validation and
persistence and allows tests to substitute a recording adapter. It introduces
an indirection and cannot enforce the behavior of a supplied implementation.
Baseline mode, crawl/total time limits, target/DNS/redirect enforcement, process
cleanup and exit-code interpretation require the concrete ZAP adapter. The URL
schema checks syntax, not current target authorization or network boundaries.

This supports FR-017 message checks and NFR-018 module separation. No scanner
command is introduced under NFR-008. Full FR-018 and AC-018 acceptance remains
dependent on subsequent execution and target-enforcement stories. Adding a tool
would require its own typed parameters, routing and handler/adapter; changing this
contract alone does not enable another scanner.

## Verification

`tests/unit/test_zap_worker.py` rejects malformed/versioned/unknown-field messages
without accessing storage or the adapter. `tests/integration/test_zap_worker.py`
uses migrated disposable PostgreSQL schemas and the shared version-one message
fixture to verify saved-job matching, committed start visibility from a separate
session, unchanged ownership, terminal/cancellation exclusion, competing deliveries,
redelivery, precommit rollback, and adapter exceptions after commit. The test
adapter performs no network requests or real scanning. See the
[test guide](../../tests/README.md#us-0071-worker-validation-and-adapter-handoff)
for commands and actual outcomes.
