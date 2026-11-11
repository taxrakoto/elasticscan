"""Baseline command and cleanup checks without launching ZAP."""
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.scanners.base import ScanRequest
from app.scanners.baseline import ZapBaselineAdapter
from app.scanners import baseline_hooks


class BaselineTests(unittest.TestCase):
    def request(self):
        return ScanRequest(
            job_id=uuid4(),
            url='https://approved.example.invalid/',
            claim_token=uuid4(),
            scope_regex=r'\Ahttps://approved\.example\.invalid/.*\Z',
        )

    def test_command_is_baseline_only_and_returns_raw_output(self):
        process = Mock(pid=12345)
        process.wait.return_value = 2
        observed = {}

        def launch(command, **options):
            observed.update(command=command, options=options)
            work = Path(options['env']['ELASTICSCAN_BASELINE_DIR'])
            (work / 'report.json').write_text('{"site": []}')
            (work / 'evidence.json').write_text(json.dumps({
                'passive_pending': 0,
                'active_scan_count': 0,
                'visited_urls': [],
            }))
            return process

        with patch(
            'app.scanners.baseline.subprocess.Popen',
            side_effect=launch,
        ), patch('app.scanners.baseline.os.killpg') as kill:
            result = ZapBaselineAdapter(
                crawl_minutes=2, total_timeout_seconds=300,
            ).run(self.request())

        command = observed['command']
        self.assertIn('/zap/zap-baseline.py', command)
        self.assertIn('--autooff', command)
        self.assertEqual(command[command.index('-m') + 1], '2')
        self.assertNotIn('-U', command)
        self.assertNotIn('-j', command)
        self.assertNotIn('shell', observed['options'])
        self.assertTrue(observed['options']['start_new_session'])
        self.assertNotIn('PGPASSWORD', observed['options']['env'])
        self.assertEqual(result.return_code, 2)
        self.assertTrue(result.passive_complete)
        self.assertEqual(result.report, '{"site": []}')
        kill.assert_called_once()
        self.assertFalse(Path(observed['options']['cwd']).exists())

    def test_timeout_kills_group_and_removes_temporary_files(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [
            subprocess.TimeoutExpired('baseline', 2),
            -9,
        ]
        observed = {}

        def launch(command, **options):
            observed.update(options)
            return process

        with patch(
            'app.scanners.baseline.subprocess.Popen',
            side_effect=launch,
        ), patch('app.scanners.baseline.os.killpg') as kill:
            with self.assertRaises(TimeoutError):
                ZapBaselineAdapter(
                    total_timeout_seconds=2,
                ).run(self.request())

        kill.assert_called_once()
        self.assertFalse(Path(observed['cwd']).exists())

    def test_invalid_budgets_are_rejected(self):
        for value in (0, -1, True, '1', 1.5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ZapBaselineAdapter(crawl_minutes=value)
                with self.assertRaises(ValueError):
                    ZapBaselineAdapter(total_timeout_seconds=value)

    def test_startup_replaces_packaged_wildcard_defaults(self):
        common = Mock()
        with patch.dict('sys.modules', {'zap_common': common}):
            baseline_hooks.start_zap(8080, [])
        command = common.create_start_options(
            '-daemon', 8080, ['-host', '127.0.0.1'],
        )
        self.assertNotIn('0.0.0.0', command)
        self.assertNotIn('api.addrs.addr.name=.*', command)
        self.assertEqual(command.count('-host'), 1)

    def test_scope_is_applied_before_initial_access(self):
        zap = Mock()
        request = self.request()
        zap.context.new_context.return_value = '1'
        zap.context.include_in_context.return_value = 'OK'
        zap.context.set_context_in_scope.return_value = 'OK'

        with patch.object(
            baseline_hooks, 'saved_request', return_value=request,
        ), patch.dict('sys.modules', {'zap_common': Mock()}):
            baseline_hooks.zap_started(zap, request.url)

        zap.urlopen(request.url)
        zap.urlopen.func.assert_called_once_with(
            request.url, allow_redirects=False,
        )
        zap.spider.scan.assert_not_called()
        zap.ascan.scan.assert_not_called()

    def test_unfinished_passive_queue_or_active_scan_is_rejected(self):
        request = self.request()
        for pending, active in ((1, []), (0, [{'id': '1'}])):
            with self.subTest(pending=pending, active=active):
                zap = Mock()
                zap.pscan.records_to_scan = pending
                zap.ascan.scans = active
                zap.core.urls.return_value = [request.url]
                with patch.object(
                    baseline_hooks, 'saved_request', return_value=request,
                ):
                    with self.assertRaises(RuntimeError):
                        baseline_hooks.zap_pre_shutdown(zap)
                zap.ascan.scan.assert_not_called()
