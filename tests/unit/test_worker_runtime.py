"""Worker configuration and interruption checks without external services."""
import os
import subprocess
import unittest
from threading import Event
from unittest.mock import Mock, call, patch

from sqlalchemy.exc import OperationalError

from app.scanners.base import ScanInterrupted
from app.scanners.baseline import ZapBaselineAdapter, wait_for_process
from app.scanners.zap_outcomes import execute_zap_scan
from app.workers.main import run
from app.workers.recovery import recovery_loop
from app.workers.settings import WorkerSettings
from app.workers.zap import parse_message


class WorkerRuntimeTests(unittest.TestCase):
    def test_provisional_total_runtime_default_and_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                ZapBaselineAdapter.from_environment().total_timeout_seconds,
                900,
            )
        with patch.dict(os.environ, {'ZAP_TOTAL_TIMEOUT_SECONDS': '2'}):
            self.assertEqual(
                ZapBaselineAdapter.from_environment().total_timeout_seconds,
                2,
            )

    def test_recovery_configuration_and_environment(self):
        for changes in (
            {'recovery_interval_seconds': 0},
            {'recovery_interval_seconds': True},
            {'recovery_interval_seconds': 301},
            {'recovery_batch_size': 0},
            {'recovery_batch_size': True},
            {'recovery_batch_size': 101},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    WorkerSettings(**changes)

        with patch.dict(os.environ, {
            'WORKER_LEASE_SECONDS': '60',
            'WORKER_HEARTBEAT_SECONDS': '15',
            'WORKER_VISIBILITY_SECONDS': '90',
            'WORKER_RECOVERY_INTERVAL_SECONDS': '2',
            'WORKER_RECOVERY_BATCH_SIZE': '3',
        }):
            settings = WorkerSettings.from_environment()
        self.assertEqual(settings.recovery_interval_seconds, 2)
        self.assertEqual(settings.recovery_batch_size, 3)

    def test_recovery_survives_storage_failure_and_stops(self):
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.side_effect = [False, True]
        settings = WorkerSettings(recovery_interval_seconds=2)

        with patch('app.workers.recovery.recover_once') as recover:
            recover.side_effect = [
                OperationalError('fixture', {}, Exception('private detail')),
                [],
            ]
            recovery_loop(Mock(), settings=settings, stopped=stopped)

        self.assertEqual(recover.call_count, 2)
        self.assertEqual(stopped.wait.call_args_list, [call(2), call(2)])

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
