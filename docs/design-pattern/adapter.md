# Adapter

**Status:** US-007.1 implements a reusable worker handoff boundary and tests it
with a recording adapter. US-007.2 adds reusable bounded ZAP baseline execution;
US-007.3 adds reusable ZAP outcome interpretation.
US-014.2 adds a production execution wrapper and sequential queue process in
source; running containers remain unchanged until recreated.

## Problem and components

Message validation and database claims should not depend on scanner commands.
`app/scanners/base.py` defines `ScannerAdapter.run(request) -> ScannerOutput | None` and the
frozen `ScanRequest(job_id, url, claim_token, scope_regex)` value object (token added
in US-014.1, scope added in the US-005.2 application phase). A Protocol describes the method
an implementation must provide without requiring inheritance. The caller injects
both the adapter and session factory into `app/workers/zap.py:handle_message`.
This reuses [Dependency Injection](dependency-injection.md) and the existing
[Repository](repository.md) lifecycle functions. The contract is the Adapter
extension point; US-007.2 supplies the concrete process adapter described below.

US-007.2's `ZapBaselineAdapter` implements the actual process interface using the
packaged baseline script. `ScannerOutput` retains raw exit code/report and scope,
passive and active-scan evidence without interpreting findings as runtime failure.
Hooks adapt the existing context setup to the packaged spider. The worker handler
returns only `invoked`; US-014.2 supplies an `Execution` wrapper that interprets
and persists outcomes before returning through that handoff.

## Interaction and decisions

1. Parse the raw JSON body and require integer schema version 1 (not a Boolean,
   decimal or string). Validate all fields with the shared `JobMessage` schema.
2. Load the saved job by UUID. Compare scan type and the entire parameter object
   with the message's JSON-serialized typed parameters. Accepted jobs store that
   same normalized representation; differently serialized saved URLs are rejected.
3. Recheck the target against the current single-origin policy and public-address
   resolution, generating the exact scope rule. A denial returns `target_denied`
   without claiming or mutating the job.
4. Call `claim_job` to atomically claim pending work with a fresh token and lease,
   excluding cancellation requests, competing starts and nonpending jobs. Commit
   the transaction. `lease_seconds` defaults to a provisional 60 seconds.
5. Only after successful commit, call the adapter with the saved job ID and
   validated URL plus committed execution token and generated scope rule. These are internal metadata,
   not part of the target URL or scanner command. No untrusted command or
   ownership field is forwarded.

| Decision | Meaning |
| --- | --- |
| `invalid_message` | JSON/schema/version rejected before storage access |
| `missing_job` | No saved job; never reconstruct one from the message |
| `inconsistent_message` | Saved scan type or complete parameters disagree |
| `target_denied` | Empty current configuration, disallowed origin or rejected public-address resolution; no claim |
| `not_started` | Atomic start guard did not match |
| `invoked` | Adapter returned; this is not a completed scan outcome |

These decisions are not queue acknowledgment instructions. The function does not
receive an SQS receipt handle, poll a queue, delete messages, or persist terminal
results. Database and adapter exceptions propagate. A precommit failure prevents
invocation; connection loss during commit can leave the saved start uncertain.
An adapter failure or process crash after commit leaves the job running, with no
automatic retry from this raw handoff. US-014.2 adds periodic renewal and the
partial US-014.3 phase fails expired claims without running-job reclamation. See
[Execution claims and leases](../execution-leases.md) for token-protected writes,
expiry and ownership-loss handling. Passing the token does not itself stop a
scanner process when authority expires.
Pending cancellation metadata blocks starts, but running cancellation and deletion
tombstones are not implemented here. Concurrent deletion/reclamation is outside
this foundation. No exactly-once physical execution guarantee is claimed.

## Benefits, tradeoffs and acceptance

The small contract separates scanner-specific behavior from validation and
persistence and allows tests to substitute a recording adapter. It introduces
an indirection and cannot enforce the behavior of a supplied implementation.
Baseline mode, separate crawl/total limits and process cleanup are implemented by
the reusable US-007.2 adapter. US-007.3 interprets its output separately;
full redirect/crawl enforcement remains subsequent acceptance work. Worker
target authorization now reuses the configured policy, but connection-time DNS
and enforceable outbound network restrictions remain pending.

`app/scanners/scope.py:configure_scope` provides reusable scanner-specific context
setup. It accepts the committed request and an injected fresh ZAP client, configures
the rule and returns the context name without making target requests. The future
adapter must apply it before crawling and pass the name to the spider. US-007.2's
baseline hooks now do this and verify discovered-link behavior in real ZAP;
earlier unit checks use recording clients. See
[Single fixed scanner origin](../scanner-scope.md) for the reduced scope and
unsatisfied original US-005.2 acceptance requirements.

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

## Bounded execution (US-007.2)

`app/scanners/baseline.py` adapts an immutable request to a fixed command argument
list, a bounded child process group and raw immutable output. The packaged script
and Python image dependencies are coupled to the pinned ZAP runtime; tests must
verify that integration when changing the base image. Unit mocks alone cannot
prove actual scanner behavior. The explicit worker Dockerfile excludes unrelated
application modules and is import-tested without mounting source.

The caller gets no normalized severity summary or durable completion promise.
Process deadlines and cleanup add lifecycle responsibilities; fresh temporary
homes isolate per-call artifacts but parent-crash recovery remains separate.
See [ZAP baseline](../zap-baseline.md) for hooks, provisional budgets, raw output,
isolated actual-scanner checks and the remaining queue/network/recovery boundaries.

## Outcome interpretation (US-007.3)

`app/scanners/zap_outcomes.py:execute_zap_scan` accepts the same injected adapter
and request, then interprets raw output through `normalize_zap_output`. It returns
the shared `ScanOutcome` and `SeverityCounts` value objects. Finding-related exits
0/1/2 may complete only with valid report and execution evidence; runtime errors
produce safe classified failures. Unexpected programming errors propagate.

The adapter remains responsible for process execution and cleanup. Interpretation
is an ordinary function, not another design pattern. This separation keeps ZAP
report/exit conventions out of shared result types and allows another tool to
have its own interpreter. The extra boundary requires tests with real ZAP reports,
not only mocked adapters. Neither the wrapper nor the current worker handler
persists outcomes. See [ZAP outcome rules](../scanner-outcomes.md) and
[Value Object](value-object.md) for contracts, tradeoffs and verification.


## Partial local integration verification (US-019.2)

`tests/integration/test_local_slice.py` supplies a simulated scanner implementing
the existing `run(request)` boundary. Fixed raw findings or an injected timeout
pass through the production ZAP outcome interpreter. The test bridge observes
committed running state before invoking that boundary, captures its normalized
outcome, and test orchestration calls the existing guarded repository writer.
The production handler still discards adapter output; neither a live consumer
nor production outcome orchestration is added.

This reuses Adapter to verify cooperation with real submission, publication,
queue delivery, storage and retrieval, while keeping target connections absent.
It cannot establish real ZAP execution or target containment. Terminal message
replay verifies the existing claim guard prevents another invocation. No new
pattern or scanner implementation is introduced. See the
[partial local-slice guide](../local-slice.md) for interactions, six checks and
remaining US-005/US-019.2 boundaries.


## Production execution and interruption (US-014.2)

`app/workers/consumer.py:Execution` implements the existing `run(request)` handoff
contract while composing the real ZAP adapter, interpreter and repository writes.
It receives queue receipts privately; `ScanRequest` and public API/message
contracts are unchanged. Its initial pulse must commit authority before creating
the scanner, and its heartbeat extends visibility before lease renewal.

`ZapBaselineAdapter` accepts an optional stop event; callers without it preserve
the previous wait behavior. With it, bounded waits check interruption every 0.2
seconds. `ScanInterrupted` propagates through the outcome interpreter, invoking
existing process-group and temporary-directory cleanup rather than storing a
fabricated scanner failure. Shutdown and renewal uncertainty stop execution;
a current claim still guards any result write. Generic injected adapters must
honor their supplied stop event; the protocol alone cannot force cleanup.

This is Adapter reuse, with extra lifecycle coordination and dependence on the
pinned scanner runtime. New real-ZAP verification checks actual process/resource
cleanup on interruption. PostgreSQL/LocalStack tests use a controlled adapter to
prove delivery coordination, failures and competition. See
[Worker delivery](../worker-delivery.md). The older US-019.2 test bridge remains
test-only; this source now provides independent production composition. Full
US-005 and US-017.1 acceptance is not implied.
