"""Actual ZAP baseline scans against a container-local public-address fixture."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from pydantic import AnyHttpUrl

from app.core.target_policy import TargetPolicy
from app.scanners.base import ScanRequest
from app.scanners.baseline import ZapBaselineAdapter
from app.scanners.zap_outcomes import normalize_zap_output
from app.scanners.scope import fixed_origin_scope


@unittest.skipUnless(
    os.environ.get('RUN_ZAP_BASELINE_TESTS') == '1',
    'Use scripts/verify_zap_baseline.sh for isolated real ZAP tests',
)
class RealBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.hits.append((self.headers.get('Host'), self.path))
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(
                    b'<html><body><a href="/page">page</a>'
                    b'<a href="http://outside.example.invalid:8081/escape">'
                    b'outside</a></body></html>'
                )

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(('11.254.254.10', 8081), Handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        threading.Thread(
            target=cls.server.serve_forever, daemon=True,
        ).start()

    def request(self):
        url = AnyHttpUrl('http://approved.example.invalid:8081/')
        return ScanRequest(
            job_id=uuid4(),
            url=str(url),
            claim_token=uuid4(),
            scope_regex=fixed_origin_scope(
                url,
                TargetPolicy(
                    ['http://approved.example.invalid:8081'],
                    resolver=lambda *_: ('11.254.254.10',),
                ),
            ),
        )

    def run_adapter(self, adapter):
        original = tempfile.TemporaryDirectory
        launch = subprocess.Popen
        self.scanner_log = ''
        directories = []
        processes = []

        def recorded_launch(*args, **kwargs):
            process = launch(*args, **kwargs)
            processes.append(process)
            return process

        @contextmanager
        def diagnostic_directory(*args, **kwargs):
            with original(*args, **kwargs) as directory:
                directories.append(Path(directory))
                try:
                    yield directory
                finally:
                    log = Path(directory) / 'process.log'
                    if log.exists():
                        self.scanner_log = log.read_text(errors='replace')[-6000:]

        try:
            with patch(
                'app.scanners.baseline.tempfile.TemporaryDirectory',
                diagnostic_directory,
            ), patch(
                'app.scanners.baseline.subprocess.Popen',
                recorded_launch,
            ):
                return adapter.run(self.request())
        finally:
            for directory in directories:
                self.assertFalse(directory.exists())
            for process in processes:
                self.assertIsNotNone(process.poll())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(.05)
                else:
                    self.fail('Scanner child process group survived cleanup')

    def test_real_baseline_crawls_then_finishes_passive_analysis(self):
        self.hits.clear()
        result = self.run_adapter(ZapBaselineAdapter(
            crawl_minutes=1, total_timeout_seconds=180,
        ))

        self.assertIn(result.return_code, (0, 1, 2), self.scanner_log)
        self.assertIsNotNone(result.report)
        self.assertTrue(result.passive_complete)
        self.assertEqual(result.active_scan_count, 0)
        self.assertTrue(result.visited_urls)
        self.assertTrue(any(path == '/page' for _, path in self.hits))
        self.assertFalse(any(
            host.startswith('outside.example.invalid')
            for host, _ in self.hits
        ))
        outcome = normalize_zap_output(result)
        self.assertEqual(outcome.status, 'completed', self.scanner_log)
        self.assertIsNone(outcome.error_code)
        self.assertGreater(
            outcome.counts.high + outcome.counts.medium + outcome.counts.low,
            0,
            self.scanner_log,
        )

    def test_real_startup_is_bounded_by_total_timeout(self):
        with self.assertRaises(TimeoutError):
            self.run_adapter(ZapBaselineAdapter(
                crawl_minutes=1, total_timeout_seconds=1,
            ))
