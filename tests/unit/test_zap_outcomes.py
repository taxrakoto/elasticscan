"""Finding-related exits and execution failures have different outcomes."""
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock
from uuid import uuid4

from app.scanners.base import ScanRequest, ScannerOutput, SeverityCounts
from app.scanners.zap_outcomes import execute_zap_scan, normalize_zap_output


def report(*risks):
    return json.dumps({
        'site': [{
            'alerts': [
                {
                    'riskcode': risk,
                    'instances': [
                        {'uri': 'https://approved.example/page'},
                        {'uri': 'https://approved.example/other'},
                    ],
                }
                for risk in risks
            ],
        }],
    })


def output(**changes):
    return replace(
        ScannerOutput(
            return_code=0,
            report=report(),
            passive_complete=True,
            active_scan_count=0,
            visited_urls=('https://approved.example/',),
        ),
        **changes,
    )


class ZapOutcomeTests(unittest.TestCase):
    def test_finding_exit_codes_can_complete(self):
        for code in (0, 1, 2):
            with self.subTest(code=code):
                result = normalize_zap_output(output(
                    return_code=code,
                    report=report('3', '2', '2', '1', '0'),
                ))
                self.assertEqual(result.status, 'completed')
                self.assertEqual(result.counts, SeverityCounts(1, 2, 1))
                self.assertIsNone(result.error_code)
                self.assertIsNone(result.error_message)

    def test_empty_alerts_produce_zero_counts(self):
        result = normalize_zap_output(output())
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.counts, SeverityCounts())

    def test_execution_exit_codes_fail_even_with_a_report(self):
        for code in (3, 4, -9, True):
            with self.subTest(code=code):
                result = normalize_zap_output(output(return_code=code))
                self.assertEqual(result.status, 'failed')
                self.assertIsNone(result.counts)
                self.assertEqual(result.error_code, 'scanner_execution_failed')

    def test_incomplete_or_active_execution_fails(self):
        cases = (
            {'passive_complete': False},
            {'active_scan_count': None},
            {'active_scan_count': True},
            {'active_scan_count': 1},
            {'visited_urls': ()},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = normalize_zap_output(output(**changes))
                self.assertEqual(result.status, 'failed')
                self.assertIsNone(result.counts)
                self.assertEqual(result.error_code, 'scanner_execution_invalid')

    def test_missing_output_fails(self):
        result = normalize_zap_output(None)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error_code, 'scanner_output_invalid')

    def test_malformed_reports_fail(self):
        reports = (
            None, '', '{', 'null', '[]', '{}', '{"site":[]}',
            '{"site":[null]}', '{"site":[{}]}',
            '{"site":[{"alerts":null}]}',
            '{"site":[{"alerts":[null]}]}',
            '{"site":[{"alerts":[{}]}]}',
            report('4'), report(2), report(True),
        )
        for raw in reports:
            with self.subTest(raw=raw):
                result = normalize_zap_output(output(report=raw))
                self.assertEqual(result.status, 'failed')
                self.assertIsNone(result.counts)
                self.assertEqual(result.error_code, 'scanner_output_invalid')

    def test_execution_wrapper_returns_normalized_findings(self):
        adapter = Mock()
        adapter.run.return_value = output(return_code=2, report=report('2'))
        request = self.request()
        result = execute_zap_scan(adapter, request)
        adapter.run.assert_called_once_with(request)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.counts, SeverityCounts(medium=1))

    def test_expected_exceptions_are_safe_classified_failures(self):
        cases = (
            (TimeoutError('private details'), 'scanner_timeout'),
            (OSError('private details'), 'scanner_execution_failed'),
            (
                json.JSONDecodeError('private details', '{', 0),
                'scanner_output_invalid',
            ),
            (UnicodeError('private details'), 'scanner_output_invalid'),
        )
        for exception, expected in cases:
            with self.subTest(exception=type(exception).__name__):
                adapter = Mock()
                adapter.run.side_effect = exception
                result = execute_zap_scan(adapter, self.request())
                self.assertEqual(result.status, 'failed')
                self.assertIsNone(result.counts)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn('private details', result.error_message)

    def test_unexpected_programming_errors_propagate(self):
        adapter = Mock()
        adapter.run.side_effect = RuntimeError('unexpected bug')
        with self.assertRaises(RuntimeError):
            execute_zap_scan(adapter, self.request())

    @staticmethod
    def request():
        return ScanRequest(
            job_id=uuid4(),
            url='https://approved.example/',
            claim_token=uuid4(),
            scope_regex=r'\Ahttps://approved[.]example/.*\Z',
        )
