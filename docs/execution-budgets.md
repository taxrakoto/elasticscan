# Execution budgets (US-017.1)

The worker source and Compose configuration enforce provisional execution budgets.
They take effect when an operator starts newly built workers; updating source or
building an image does not replace running containers. No production capacity or
API latency acceptance is implied. The benchmark remains US-017.3.

## Capacity and configuration

| Setting | Default | Scope |
| --- | --- | --- |
| `WORKER_REPLICAS` | 1 | Compose worker replicas; one scan at a time per process |
| `WORKER_CPUS` | 1.0 | Docker CPU quota per worker container |
| `WORKER_MEMORY_LIMIT` | 2g | Docker memory limit per worker container |
| `ZAP_TOTAL_TIMEOUT_SECONDS` | 900 | Total scanner subprocess runtime, including startup, crawl and passive analysis |
| `ZAP_CRAWL_MINUTES` | 1 | Baseline crawl budget, still covered by total timeout |
| `WORKER_RECOVERY_INTERVAL_SECONDS` | 5 | Delay between completed recovery passes; integer 1–300 |
| `WORKER_RECOVERY_BATCH_SIZE` | 10 | Expired claims per transaction; integer 1–100 |

These values remain provisional. Compose validates resource configuration; worker
and adapter constructors reject invalid timing values. `.env.example` documents
the settings; an existing private `.env` is not automatically rewritten. Defaults
still apply when new variables are absent. A command-line `--scale zap-worker=N`
can override the configured replica count. Independent deployments or manually
started containers are outside this local concurrency limit.

The current policy permits **one automatic scanner attempt per job**. This is a
fixed conservative cap, not a configurable scanner retry count. US-015.1 adds
bounded retries for selected repeatable infrastructure operations; see [worker retries](worker-retries.md). Recovery never returns a job to
pending and never invokes a scanner.

## Timeout and termination

`app/scanners/baseline.py:ZapBaselineAdapter` starts the packaged baseline in its
own process group. The total timeout covers the subprocess, including startup.
Its `finally` block kills the process group and removes temporary files. A healthy
worker interprets `TimeoutError` as a canonical `scanner_timeout`, durably stores
failed state, then confirms the terminal state before acknowledging the delivery.
Cleanup and persistence add time after the configured subprocess deadline; 900
seconds is not an end-to-end request or job-completion SLA.

A memory-limit kill or hard worker crash cannot run Python cleanup. Container
teardown is needed to stop any surviving child processes. A surviving or restarted
worker resolves an expired claim through the conservative recovery path. Recovery
does not prove that an old physical scanner has stopped; it revokes database write
authority and does not initiate another scan.

## Independent recovery

`app/workers/main.py:run` starts `app/workers/recovery.py:recovery_loop` in a daemon
thread with separate sessions. It continues while the queue consumer scans or
long-polls. Each transaction calls the existing repository operation with the
configured batch size. Only committed decisions produce job-correlated success
logs. SQLAlchemy failures roll back and the next pass retries after the interval.
Shutdown interrupts the wait; the queue loop signals stop and joins recovery for
up to ten seconds. A blocked recovery database call may outlive that join; the
main worker configures connection, statement and lock timeouts. Process exit can
abandon that daemon thread and its transaction, which remains subject to normal
database rollback/recovery behavior.

With healthy storage, an available worker, sufficient batch capacity and no row
contention, eligible work is picked up within approximately a recovery interval
plus transaction scheduling/processing after lease expiry. Backlog, row locks,
worker unavailability or storage outages extend this period. It is not a fixed
wall-clock recovery guarantee during outages. Cancellation-requested jobs remain
excluded for the future cancellation finalizer; legacy unleased running jobs
still require operator handling.

## Operator procedure

Configure the desired values in local deployment configuration, then deliberately
build/start workers only for authorized targets. For example, from the app root:

```sh
docker compose config --quiet
docker compose build zap-worker
docker compose up -d --no-deps zap-worker
docker compose ps zap-worker
docker compose logs --tail 100 zap-worker
```

Inspect effective runtime limits without printing container environments:

```sh
docker inspect --format '{{.Name}} cpu={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}}' CONTAINER_ID
```

A CPU value of 1000000000 and memory value of 2147483648 represent the provisional
1 CPU / 2 GiB defaults. For stalled work, inspect status, attempt_count,
lease_expires_at and cancellation_requested_at. Restore dependency/worker health
and watch `worker_recovery_failed job_id=... policy=fail_without_rescan`. Do not
manually reset attempt counts or reopen terminal rows to force another scan.
Controlled redrive remains US-015.3.

## Verification boundaries

[Worker delivery](worker-delivery.md) and the [test guide](../tests/README.md)
cover real PostgreSQL/LocalStack checks for killed-worker recovery, real lease
expiry, batching, competing recovery, rollback, busy-scanner independence, stale
writers, cancellation metadata and absent records. These scanner adapters are
simulated and do not contact targets. Real ZAP separately runs in a network-none
fixture under CPU/memory limits and verifies completion, shortened timeout and
interruption cleanup. Default/environment checks verify the 900-second setting;
a shortened real run is not a literal fifteen-minute execution. No external scan,
production deployment, full deletion/cancellation lifecycle or API benchmark is
claimed by these checks.


## Infrastructure-operation budget

US-015.1 adds `WORKER_RETRY_MAX_ATTEMPTS=3` and
`WORKER_RETRY_BASE_DELAY_SECONDS=2`. Defaults permit two retry waits (two/four
seconds), independently of the one-scanner-attempt policy. A restart resets this
operation-local budget. Claims, scanner execution, terminal writes and authority
renewal are not wrapped. See [worker retries](worker-retries.md) for classification,
shutdown, safe diagnostics and the existing supervisor's restart behavior.
