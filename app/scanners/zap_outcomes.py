"""Interpret ZAP baseline output without updating stored jobs."""
import json

from app.scanners.base import (
    ScanOutcome, ScannerAdapter, ScannerOutput, ScanRequest, SeverityCounts,
)


def failure(code: str, message: str) -> ScanOutcome:
    return ScanOutcome(counts=None, error_code=code, error_message=message)


def normalize_zap_output(output: ScannerOutput | None) -> ScanOutcome:
    """Accept findings only after successful execution and valid evidence."""
    if output is None:
        return failure(
            'scanner_output_invalid',
            'Scanner output was missing or invalid.',
        )

    if type(output.return_code) is not int or output.return_code not in (0, 1, 2):
        return failure('scanner_execution_failed', 'Scanner execution failed.')

    if (
        output.passive_complete is not True
        or type(output.active_scan_count) is not int
        or output.active_scan_count != 0
        or not output.visited_urls
    ):
        return failure(
            'scanner_execution_invalid',
            'Scanner execution did not satisfy the baseline requirements.',
        )

    try:
        if not isinstance(output.report, str):
            raise ValueError('Missing report')
        report = json.loads(output.report)
        if not isinstance(report, dict):
            raise ValueError('Invalid report')
        sites = report.get('site')
        if not isinstance(sites, list) or not sites:
            raise ValueError('Missing sites')

        counts = {'high': 0, 'medium': 0, 'low': 0}
        severity_names = {'0': None, '1': 'low', '2': 'medium', '3': 'high'}
        for site in sites:
            if not isinstance(site, dict):
                raise ValueError('Invalid site')
            alerts = site.get('alerts')
            if not isinstance(alerts, list):
                raise ValueError('Invalid alerts')
            for alert in alerts:
                if not isinstance(alert, dict):
                    raise ValueError('Invalid alert')
                risk = alert.get('riskcode')
                if not isinstance(risk, str) or risk not in severity_names:
                    raise ValueError('Invalid severity')
                name = severity_names[risk]
                if name is not None:
                    counts[name] += 1
    except ValueError:
        return failure(
            'scanner_output_invalid',
            'Scanner output was missing or invalid.',
        )

    return ScanOutcome(counts=SeverityCounts(**counts))


def execute_zap_scan(
    adapter: ScannerAdapter,
    request: ScanRequest,
) -> ScanOutcome:
    """Execute and interpret one ZAP scan; persist nothing."""
    try:
        output = adapter.run(request)
    except TimeoutError:
        return failure(
            'scanner_timeout',
            'Scanner execution exceeded its time limit.',
        )
    except OSError:
        return failure('scanner_execution_failed', 'Scanner execution failed.')
    except (json.JSONDecodeError, UnicodeError):
        return failure(
            'scanner_output_invalid',
            'Scanner output was missing or invalid.',
        )
    return normalize_zap_output(output)
