"""Failure classification and bounded retry behavior without services."""
import os
import unittest
from threading import Event
from unittest.mock import Mock, call, patch

from botocore.exceptions import (
    ClientError, EndpointConnectionError, NoCredentialsError,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from app.workers.main import consume_loop
from app.workers.retry import RetryStopped, retry_category, retry_operation
from app.workers.settings import WorkerSettings


def database_error(state):
    original = Exception('private database detail')
    original.sqlstate = state
    return OperationalError('private SQL', {}, original)


class WorkerRetryTests(unittest.TestCase):
    def settings(self, **changes):
        return WorkerSettings(**changes)

    def test_classification(self):
        for error in (
            EndpointConnectionError(endpoint_url='http://private.invalid'),
            ClientError({'Error': {'Code': 'Throttling'}}, 'ReceiveMessage'),
            ClientError(
                {'ResponseMetadata': {'HTTPStatusCode': 503}},
                'ReceiveMessage',
            ),
            database_error(None),
            database_error('08006'),
            database_error('40001'),
            database_error('40P01'),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsNotNone(retry_category(error))

        for error in (
            NoCredentialsError(),
            ClientError({'Error': {'Code': 'AccessDenied'}}, 'ReceiveMessage'),
            database_error('28P01'),
            database_error('42P01'),
            IntegrityError('private SQL', {}, Exception('private')),
            ValueError('private programming detail'),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertIsNone(retry_category(error))

    def test_transient_success_uses_bounded_delays(self):
        operation = Mock(side_effect=[
            database_error('08006'), database_error('08006'), 'stored',
        ])
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        result = retry_operation(
            operation, name='confirm_terminal',
            settings=self.settings(), stopped=stopped,
        )
        self.assertEqual(result, 'stored')
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(stopped.wait.call_args_list, [call(2), call(4)])

    def test_exhaustion_preserves_final_exception_without_extra_wait(self):
        error = database_error('08006')
        operation = Mock(side_effect=error)
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        with self.assertRaises(OperationalError) as raised:
            retry_operation(
                operation, name='confirm_terminal',
                settings=self.settings(), stopped=stopped,
            )
        self.assertIs(raised.exception, error)
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(stopped.wait.call_args_list, [call(2), call(4)])

    def test_permanent_error_is_not_retried_and_logs_are_safe(self):
        operation = Mock(side_effect=ValueError('secret-password'))
        stopped = Event()
        with self.assertLogs('app.workers.retry', level='ERROR') as logs:
            with self.assertRaises(ValueError):
                retry_operation(
                    operation, name='fixture',
                    settings=self.settings(), stopped=stopped,
                )
        operation.assert_called_once()
        self.assertNotIn('secret-password', '\n'.join(logs.output))

    def test_shutdown_prevents_initial_and_subsequent_attempts(self):
        stopped = Event()
        stopped.set()
        operation = Mock()
        with self.assertRaises(RetryStopped):
            retry_operation(
                operation, name='fixture',
                settings=self.settings(), stopped=stopped,
            )
        operation.assert_not_called()
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = True
        operation.side_effect = database_error('08006')
        with self.assertRaises(RetryStopped):
            retry_operation(
                operation, name='fixture',
                settings=self.settings(), stopped=stopped,
            )
        operation.assert_called_once()

    def test_delay_cap_and_single_attempt(self):
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        operation = Mock(side_effect=database_error('08006'))
        with self.assertRaises(OperationalError):
            retry_operation(
                operation, name='fixture',
                settings=self.settings(
                    retry_max_attempts=5, retry_base_delay_seconds=30,
                ),
                stopped=stopped,
            )
        self.assertEqual(
            stopped.wait.call_args_list,
            [call(30), call(60), call(60), call(60)],
        )
        stopped.wait.reset_mock()
        operation.reset_mock()
        with self.assertRaises(OperationalError):
            retry_operation(
                operation, name='fixture',
                settings=self.settings(retry_max_attempts=1), stopped=stopped,
            )
        operation.assert_called_once()
        stopped.wait.assert_not_called()

    def test_settings_validation_and_environment(self):
        for changes in (
            {'retry_max_attempts': 0},
            {'retry_max_attempts': True},
            {'retry_max_attempts': 11},
            {'retry_base_delay_seconds': 0},
            {'retry_base_delay_seconds': True},
            {'retry_base_delay_seconds': 31},
        ):
            with self.assertRaises(ValueError):
                self.settings(**changes)
        with patch.dict(os.environ, {
            'WORKER_RETRY_MAX_ATTEMPTS': '4',
            'WORKER_RETRY_BASE_DELAY_SECONDS': '3',
        }):
            settings = WorkerSettings.from_environment()
        self.assertEqual(settings.retry_max_attempts, 4)
        self.assertEqual(settings.retry_base_delay_seconds, 3)

    def test_receive_exhaustion_stops_consumer(self):
        sqs = Mock()
        sqs.receive_message.side_effect = EndpointConnectionError(
            endpoint_url='http://private.invalid',
        )
        stopped = Mock()
        stopped.is_set.return_value = False
        stopped.wait.return_value = False
        consume_loop(
            Mock(), sqs=sqs, queue_url='fixture', worker_id='worker-a',
            settings=self.settings(), stopped=stopped,
            adapter_factory=Mock(),
        )
        self.assertEqual(sqs.receive_message.call_count, 3)
        self.assertEqual(stopped.wait.call_args_list, [call(2), call(4)])
