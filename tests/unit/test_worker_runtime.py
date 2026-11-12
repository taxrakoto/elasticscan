"""Worker configuration and interruption checks without external services."""
import subprocess
import unittest
from threading import Event
from unittest.mock import Mock

from app.scanners.base import ScanInterrupted
from app.scanners.baseline import wait_for_process
from app.scanners.zap_outcomes import execute_zap_scan
from app.workers.main import run
from app.workers.settings import WorkerSettings
from app.workers.zap import parse_message


class WorkerRuntimeTests(unittest.TestCase):
    def test_timing_configuration_rejects_unsafe_combinations(self):
        self.assertEqual(WorkerSettings().visibility_seconds, 90)
        for changes in (
            {'lease_seconds': 0},
            {'heartbeat_seconds': True},
            {'heartbeat_seconds': 21},
            {'visibility_seconds': 60},
            {'visibility_seconds': 43201},
            {'lease_seconds': 3601},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    WorkerSettings(**changes)

    def test_invalid_messages_are_rejected(self):
        for body in (
            '{', '[]', 'null',
            '{"schema_version": true}',
            '{"schema_version": 2}',
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse_message(body))

    def test_interruption_propagates_without_scanner_failure_conversion(self):
        stopped = Event()
        stopped.set()
        process = Mock()
        with self.assertRaises(ScanInterrupted):
            wait_for_process(process, 10, stopped)
        process.wait.assert_not_called()

        adapter = Mock()
        adapter.run.side_effect = ScanInterrupted('stopped')
        with self.assertRaises(ScanInterrupted):
            execute_zap_scan(adapter, Mock())

    def test_unchanged_wait_preserves_timeout_behavior(self):
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired('scanner', 1)
        with self.assertRaises(subprocess.TimeoutExpired):
            wait_for_process(process, 1)
        process.wait.assert_called_once_with(timeout=1)

    def test_stopped_worker_does_not_poll_or_open_storage(self):
        stopped = Event()
        stopped.set()
        sessions = Mock()
        sqs = Mock()
        run(
            sessions,
            sqs=sqs,
            queue_url='fixture',
            worker_id='worker-a',
            settings=WorkerSettings(),
            stopped=stopped,
            adapter_factory=Mock(),
        )
        sessions.begin.assert_not_called()
        sqs.receive_message.assert_not_called()
