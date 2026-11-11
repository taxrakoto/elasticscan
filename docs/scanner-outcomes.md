# ZAP outcome interpretation (US-007.3)

This reusable code distinguishes findings from failed execution. It does not
persist terminal jobs or change the deployed idle worker. US-008.2 and US-008.3
will connect normalized outcomes to guarded database writes.

## Calling and shared contract

```python
from dataclasses import asdict
from app.scanners.baseline import ZapBaselineAdapter
from app.scanners.zap_outcomes import execute_zap_scan

# Trusted internal request produced after authorization and a committed claim.
outcome = execute_zap_scan(ZapBaselineAdapter.from_environment(), request)
if outcome.status == 'completed':
    result = asdict(outcome.counts)  # high, medium, low
else:
    error = {'code': outcome.error_code, 'message': outcome.error_message}
```

`ScanOutcome` and `SeverityCounts` in `app/scanners/base.py` are shared immutable
value objects. Future tools can produce this same outcome contract through their
own interpreters. `zap_outcomes.py` owns ZAP-specific exit codes and report parsing.
`normalize_zap_output(output)` interprets existing raw output; `execute_zap_scan`
also runs the injected adapter and classifies expected execution exceptions.
The shared objects are internal handoffs, not request validators: trusted callers
must use the interpreter to obtain a consistent outcome. They do not establish
authorization, execution ownership, network enforcement or database completion.

## Interpretation rules

| Condition | Outcome |
| --- | --- |
| Exit 0, 1 or 2, valid report, completed passive analysis, zero active scans, nonempty visited URLs | `completed` with severity counts |
| Other exit code, including process termination | `failed`, `scanner_execution_failed` |
| Parent `TimeoutError` | `failed`, `scanner_timeout` |
| Adapter `OSError` | `failed`, `scanner_execution_failed` |
| Missing/malformed output, report, evidence JSON or text decoding failure | `failed`, `scanner_output_invalid` |
| Passive queue unfinished, missing/nonzero active count or empty visited URLs | `failed`, `scanner_execution_invalid` |

ZAP baseline exit 1 means FAIL findings, and 2 means WARN findings without FAIL.
Neither means the process itself failed. Even exit 0 requires valid execution
evidence and a report. The existing hooks establish scope evidence; the interpreter
does not independently recheck recorded URLs against the request regex.

The report must be a JSON object with a nonempty `site` list. Every site must have
an `alerts` list; an empty alerts list is valid and yields zero counts. Each alert
must have a string `riskcode`: `3` high, `2` medium, `1` low, or `0` informational.
Unknown or malformed severities reject the whole report rather than undercount.
Extra report metadata is ignored. Counts are derived from grouped alert entries,
not their `instances`, request totals, descriptive text or supplied count fields.
Each entry counts once per site; no cross-site deduplication is performed.
Informational alerts are recognized but omitted from the published three-category
summary. All returned counts are nonnegative integers; no findings is still success.

Messages are fixed, safe strings. Raw exception text, reports and process logs
are never included in the outcome. Unexpected programming exceptions propagate;
this wrapper does not hide arbitrary bugs or catch database failures. The adapter
still owns process-group and temporary-file cleanup, including when an exception
is converted into a failure outcome.

## Verification and limits

`tests/unit/test_zap_outcomes.py` covers finding exits, zero counts, grouped alert
counting, execution errors, invalid execution evidence, malformed reports, safe
exception classification and unexpected exception propagation. The existing real
baseline integration test additionally checks that actual findings normalize to
`completed`. Its timeout test verifies real process and temporary-file cleanup;
the timeout-to-outcome conversion is independently unit tested.

See the [test guide](../tests/README.md#us-0073-zap-outcome-interpretation) for
commands and recorded results. The worker Dockerfile explicitly copies only the
new interpreter alongside required modules; no whole-application copy is added.

This supports FR-017/FR-018, NFR-008/NFR-018 and the outcome portion of AC-018.
The callable worker handler still discards raw adapter output. There is no live
SQS consumer, periodic lease renewal, terminal persistence or changed API result.
Full AC-018 target enforcement remains subject to the outstanding US-005.2 AWS
network/redirect work. See [baseline execution](zap-baseline.md),
[Adapter](design-pattern/adapter.md) and [Value Object](design-pattern/value-object.md).

Sources: [ZAP baseline exit codes](https://www.zaproxy.org/docs/docker/baseline-scan/)
and [traditional JSON report](https://www.zaproxy.org/docs/desktop/addons/report-generation/report-traditional-json/).
