# Bounded ZAP baseline adapter (US-007.2)

This reusable adapter executes the packaged ZAP 2.16.1 baseline script: bounded
traditional crawling followed by waiting for passive analysis to drain. It accepts
no target login credentials or arbitrary scanner options and invokes no active
scan. The deployed worker remains an idle bootstrap process. This does not add
SQS polling, periodic execution-lease renewal, terminal persistence or API results.

## Calling the adapter

```python
from app.scanners.baseline import ZapBaselineAdapter

# Run inside the worker image with an application-generated ScanRequest.
adapter = ZapBaselineAdapter.from_environment()
output = adapter.run(request)
```

The worker's existing validation/claim code produces `ScanRequest` with job ID,
validated target URL, committed claim token and application-generated fixed-origin
regex. Direct adapter callers are trusted internal code and must establish the
same authorization, target checks and execution ownership. Neither the request
dataclass nor this adapter authenticates a caller. Never reconstruct a request
directly from an unverified queue message.

`ScannerOutput` carries the raw exit code, optional raw JSON report, passive
completion evidence, active-scan history count and recorded URLs. It is an
immutable handoff, not a normalized result or durable job completion. Codes 1/2
can represent findings in the baseline contract; they are preserved, not raised
as generic execution failures. Missing evidence/report or code 3 must not be
interpreted as a successful scan. US-007.3 adds separate [ZAP outcome interpretation](scanner-outcomes.md);
safe terminal persistence remains US-008.2/US-008.3. The current
`handle_message` discards adapter output and returns only `invoked`.

## Separate budgets

| Setting | Provisional default | Effect |
| --- | --- | --- |
| `ZAP_CRAWL_MINUTES` | 1 | Baseline `-m` traditional-spider duration |
| `ZAP_TOTAL_TIMEOUT_SECONDS` | 900 | Parent deadline for script startup, crawling, passive analysis and report generation |

Both require positive integers. `ZapBaselineAdapter` also accepts the settings as
constructor arguments. A total budget shorter than the crawl budget is allowed;
the parent deadline wins. The script's `-T` supplies an additional startup/passive
wait bound rounded down to whole minutes with a one-minute minimum; it is not the
overall deadline. The final hook checks that the passive queue actually reached
zero because the packaged script's internal passive wait may return after its
own timeout. Private `.env` files are not changed by this implementation.

The parent launches an argument list with no shell and starts a new process group.
On timeout it raises a safe `TimeoutError` and kills the scanner process group;
cleanup also kills remaining children after normal script exit. It waits up to
five seconds for the direct child to be reaped. Unexpected parent death, graceful
worker shutdown and bounded abandoned-job recovery remain subsequent integration
work. A live caller must renew its execution lease throughout long scans; the
60-second claim default is not a 15-minute execution lease.

## Scope hooks and data handling

`baseline_hooks.py` is a trusted fixed hook path, not an API option. It configures
the existing exact-origin context before initial target access and supplies its
name to the baseline spider. Initial access does not automatically follow
redirects. No AJAX spider, active scan, target user or login context is requested.
The startup hook removes the packaged wildcard listener/access defaults. The
local client requires both IPv4/IPv6 loopback and its special `zap` API Host name;
the configured listener remains loopback-only. Before shutdown, it checks passive queue zero, no active-scan history, and recorded
URLs matching the origin. Recorded out-of-origin activity causes verification to
fail; this is after-the-fact detection, not a firewall.

Each call creates a fresh temporary ZAP home, request file, report, evidence and
process log. They are removed after the output is read, including on timeout.
The scanner subprocess receives a small environment excluding inherited
database/AWS credentials and proxy settings. Runtime add-on updates are disabled
with `-silent`. ZAP's local API is bound/restricted to loopback in the dedicated
worker runtime; it is not a public endpoint. Raw reports may contain target data;
this adapter neither publishes nor logs them to clients.

## Worker image and verification

`docker/worker/Dockerfile` uses an explicit file list for the worker, scanner,
schemas, lifecycle persistence and required shared modules. API routes/main,
dispatcher, service orchestration, migrations, tests and documentation are not
copied. The existing locked dependencies are installed into the base image's
Python environment. The image stays nonroot with the original idle entry point.
Build does not replace a running worker:

```sh
docker compose build zap-worker
sh scripts/verify_zap_baseline.sh
```

The verification helper creates a disposable `--network none` container. Root
fixture setup uses NET_ADMIN and a standard-library Linux ioctl to assign
11.254.254.10 only to a local loopback alias, and writes synthetic container DNS
entries. It then drops to `zap` before running actual scans. There is no route to
the real public address, no application `.env`, no private-address policy bypass,
and no capability added to the deployed service. The entire container is removed.
The helper requires Linux in Docker and the pinned image's `setpriv` utility.

Unit tests verify command construction, local-only startup options, independent settings, raw-code retention,
scope-before-access, passive/active checks, timeout cleanup and temporary files.
Opt-in real tests verify a discovered in-origin page, passive completion, no
active scan, out-of-origin link exclusion and startup timeout. Actual process
groups and temporary directories must be gone after both completion and timeout.
Import/content
verification also runs against the built image without a source mount. See
[test guide](../tests/README.md#us-0072-bounded-baseline-execution) for actual results.

## Remaining acceptance boundaries

This follows the approved single-origin application design from
[scanner scope](scanner-scope.md). Full US-005.2 egress enforcement, DNS changes
during a connection, AWS outbound restrictions and comprehensive redirect tests
remain pending. The original requirement is not silently marked satisfied.
US-007.2 verification covers crawling/passive execution and bounded process
cleanup; US-007.3 adds normalized outcomes; full FR-018/AC-018 still needs
target-boundary enforcement and integration with durable terminal writes.
CPU/memory/concurrency policy and durable timeout failures remain US-017.1 and
the outcome/recovery stories. No gateway containers or live queue consumer are
introduced. This uses [Adapter](design-pattern/adapter.md); deadlines, process
groups and hooks are techniques, not additional design patterns.
