"""Dispatcher discovery, shutdown, and database-failure handling."""
import unittest
from datetime import timedelta
from threading import Event
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.dispatchers.main import run, run_pass
from app.dispatchers.publication import retry_delay


class DispatcherTests(unittest.TestCase):
    def sessions_with_jobs(self, job_ids):
        sessions = Mock()
        # Session factories return context managers, which need magic methods.
        sessions.return_value = MagicMock()
        session = sessions.return_value.__enter__.return_value
        session.scalars.return_value = job_ids
        return sessions

    def test_pass_publishes_discovered_due_jobs(self):
        ids = [uuid4(), uuid4()]
        sessions = self.sessions_with_jobs(ids)
        sns = Mock()
        with patch(
            'app.dispatchers.main.publish_job', return_value='published',
        ) as publish:
            result = run_pass(
                sessions, sns=sns, topic_arn='fixture-topic', stopped=Event(),
            )
        self.assertEqual(result, ['published', 'published'])
        self.assertEqual(publish.call_count, 2)
        for call, job_id in zip(publish.call_args_list, ids):
            self.assertEqual(call.kwargs, {
                'job_id': job_id,
                'sns': sns,
                'topic_arn': 'fixture-topic',
            })

    def test_shutdown_stops_before_the_next_publication(self):
        sessions = self.sessions_with_jobs([uuid4(), uuid4()])
        stopped = Event()

        def publish(*args, **kwargs):
            stopped.set()
            return 'published'

        with patch(
            'app.dispatchers.main.publish_job', side_effect=publish,
        ) as publisher:
            result = run_pass(
                sessions, sns=Mock(), topic_arn='fixture-topic', stopped=stopped,
            )
        self.assertEqual(result, ['published'])
        publisher.assert_called_once()

    def test_already_stopped_pass_does_not_access_database(self):
        sessions = Mock()
        stopped = Event()
        stopped.set()
        self.assertEqual(run_pass(
            sessions, sns=Mock(), topic_arn='fixture-topic', stopped=stopped,
        ), [])
        sessions.assert_not_called()

    def test_database_failure_waits_before_another_pass(self):
        stopped = Mock()
        stopped.is_set.side_effect = [False, True]
        with patch(
            'app.dispatchers.main.run_pass',
            side_effect=SQLAlchemyError('fixture failure'),
        ):
            with self.assertLogs('app.dispatchers.main', level='ERROR') as logs:
                run(Mock(), sns=Mock(), topic_arn='fixture-topic', stopped=stopped)
        stopped.wait.assert_called_once_with(2)
        self.assertIn('dispatcher_database_unavailable', logs.output[0])
        self.assertNotIn('fixture failure', logs.output[0])

    def test_retry_delay_has_exponential_bounds_and_a_cap(self):
        for count, ceiling in (
            (1, 2), (2, 4), (3, 8),
            (4, 16), (5, 30), (10000, 30),
        ):
            for use_upper in (False, True):
                with self.subTest(count=count, upper=use_upper):
                    def choose(lower, upper):
                        return upper if use_upper else lower

                    with patch(
                        'app.dispatchers.publication.uniform',
                        side_effect=choose,
                    ) as random_delay:
                        actual = retry_delay(count)

                    random_delay.assert_called_once_with(
                        ceiling / 2, ceiling,
                    )
                    expected = ceiling if use_upper else ceiling / 2
                    self.assertEqual(
                        actual, timedelta(seconds=expected),
                    )
