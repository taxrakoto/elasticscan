"""Real storage/queue checks for delivery coordination and recovery."""
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

import boto3
from alembic import command
from alembic.config import Config
from botocore.config import Config as AWSConfig
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError

from app.core.target_policy import TargetPolicy
from app.core.team import TeamContext
from app.db.session import database_url, make_engine, make_session_factory
from app.models import Job
from app.repositories.lifecycle import claim_job, complete_job
from app.repositories.recovery import fail_expired_claims
from app.scanners.base import ScanInterrupted, ScannerOutput, SeverityCounts
from app.schemas import ZapSubmission
from app.services.acceptance import accept_scan
from app.workers.consumer import consume_one
from app.workers.main import run
from app.workers.settings import WorkerSettings
from docker.localstack.provision import provision


ENABLED = all(
    os.environ.get(name) == '1'
    for name in ('RUN_POSTGRES_TESTS', 'RUN_LOCALSTACK_TESTS')
)


@unittest.skipUnless(ENABLED, 'Enable PostgreSQL and LocalStack tests')
class WorkerDeliveryTests(unittest.TestCase):
    def setUp(self):
        if any(os.environ.get(name) != 'http://localstack:4566'
               for name in ('SNS_ENDPOINT_URL', 'SQS_ENDPOINT_URL')):
            raise RuntimeError('Worker tests require default LocalStack endpoints')

        self.admin = make_engine()
        self.addCleanup(self.admin.dispose)
        self.schema = 'test_delivery_' + uuid4().hex
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.drop_schema)

        self.engine = create_engine(database_url(), connect_args={
            'options': f'-c search_path={self.schema} -c timezone=UTC',
        })
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        with self.engine.begin() as connection:
            config = Config('alembic.ini')
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')

        options = {
            'endpoint_url': 'http://localstack:4566',
            'region_name': 'us-east-1',
            'aws_access_key_id': 'test',
            'aws_secret_access_key': 'test',
            'config': AWSConfig(
                connect_timeout=3, read_timeout=5,
                retries={'total_max_attempts': 1},
            ),
        }
        self.sns = boto3.client('sns', **options)
        self.sqs = boto3.client('sqs', **options)
        self.addCleanup(self.sns.close)
        self.addCleanup(self.sqs.close)

        prefix = 'test-delivery-' + uuid4().hex
        names = {
            'topic_name': prefix + '-jobs',
            'queue_name': prefix + '-zap',
            'dlq_name': prefix + '-dlq',
        }
        topic = self.sns.create_topic(Name=names['topic_name'])['TopicArn']
        self.addCleanup(self.sns.delete_topic, TopicArn=topic)
        self.addCleanup(self.delete_queue, names['dlq_name'])
        self.addCleanup(self.delete_queue, names['queue_name'])
        self.resources = provision(self.sns, self.sqs, **names)

        self.policy = TargetPolicy(
            ['https://approved.example.invalid'],
            resolver=lambda *_: ('8.8.8.8',),
        )
        self.settings = WorkerSettings(
            lease_seconds=3, heartbeat_seconds=1, visibility_seconds=6,
        )
        self.stopped = Event()

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA "{self.schema}" CASCADE'
            )

    def delete_queue(self, name):
        try:
            url = self.sqs.get_queue_url(QueueName=name)['QueueUrl']
        except ClientError as error:
            if error.response['Error']['Code'] in (
                'AWS.SimpleQueueService.NonExistentQueue',
                'QueueDoesNotExist',
            ):
                return
            raise
        self.sqs.delete_queue(QueueUrl=url)

    def create(self):
        submission = ZapSubmission.model_validate({
            'scan_type': 'zap',
            'parameters': {'url': 'https://approved.example.invalid/'},
        })
        job_id = accept_scan(
            self.sessions,
            submission=submission,
            context=TeamContext('team-alpha', 'alice'),
        ).job_id
        body = json.dumps({
            'schema_version': 1,
            'job_id': str(job_id),
            **submission.model_dump(mode='json'),
        })
        return job_id, body

    def publish(self, body):
        self.sns.publish(
            TopicArn=self.resources['topic_arn'],
            Message=body,
            MessageAttributes={
                'scan_type': {'DataType': 'String', 'StringValue': 'zap'},
            },
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            messages = self.sqs.receive_message(
                QueueUrl=self.resources['queue_url'],
                MaxNumberOfMessages=1,
                WaitTimeSeconds=1,
                VisibilityTimeout=6,
            ).get('Messages', [])
            if messages:
                self.assertEqual(messages[0]['Body'], body)
                return messages[0]
        self.fail('Fixture delivery did not arrive')

    def output(self):
        return ScannerOutput(
            return_code=2,
            report='{"site":[{"alerts":[{"riskcode":"3"}]}]}',
            passive_complete=True,
            active_scan_count=0,
            visited_urls=('https://approved.example.invalid/',),
        )

    def consume(self, delivery, factory, worker='worker-a', sqs=None):
        return consume_one(
            delivery,
            sessions=self.sessions,
            sqs=self.sqs if sqs is None else sqs,
            queue_url=self.resources['queue_url'],
            worker_id=worker,
            settings=self.settings,
            stopped=self.stopped,
            adapter_factory=factory,
            policy=self.policy,
        )

    def expire(self, job_id):
        with self.sessions.begin() as session:
            session.get(Job, job_id).lease_expires_at = (
                session.scalar(select(func.clock_timestamp()))
                - timedelta(seconds=1)
            )

    def test_heartbeat_completion_and_terminal_duplicate(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        entered = Event()
        release = Event()
        adapter = Mock()

        def scan(request):
            entered.set()
            if not release.wait(15):
                raise RuntimeError('Test scanner was not released')
            return self.output()

        adapter.run.side_effect = scan
        tracked = Mock(wraps=self.sqs)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.consume, delivery, lambda _: adapter, 'worker-a', tracked
            )
            try:
                self.assertTrue(entered.wait(5))
                with self.sessions() as session:
                    initial = session.get(Job, job_id)
                    initial_heartbeat = initial.heartbeat_at
                    initial_expiry = initial.lease_expires_at
                # Hold past both original windows (lease 3s, visibility 6s).
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    self.assertFalse(future.done())
                    release.wait(0.1)
                with self.sessions() as session:
                    job = session.get(Job, job_id)
                    self.assertEqual(job.status, 'running')
                    self.assertGreater(job.heartbeat_at, initial_heartbeat)
                    self.assertGreater(job.lease_expires_at, initial_expiry)
                self.assertGreaterEqual(
                    tracked.change_message_visibility.call_count, 2
                )
                self.assertEqual(self.sqs.receive_message(
                    QueueUrl=self.resources['queue_url'],
                    WaitTimeSeconds=0,
                ).get('Messages', []), [])
                tracked.delete_message.assert_not_called()
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), 'processed')

        tracked.delete_message.assert_called_once()
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.result, {'high': 1, 'medium': 0, 'low': 0})
            self.assertEqual(job.attempt_count, 1)
            self.assertEqual((job.team_id, job.created_by), ('team-alpha', 'alice'))

        duplicate = self.publish(body)
        factory = Mock()
        self.assertEqual(
            self.consume(duplicate, factory, 'worker-b'),
            'terminal_confirmed',
        )
        factory.assert_not_called()

    def test_safe_scanner_failure_is_committed_before_acknowledgment(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        adapter = Mock()
        adapter.run.side_effect = TimeoutError('password=secret')
        self.assertEqual(
            self.consume(delivery, lambda _: adapter), 'processed'
        )
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, 'failed')
            self.assertEqual(job.error['code'], 'scanner_timeout')
            self.assertNotIn('password', json.dumps(job.error))
            self.assertIsNone(job.result)

    def test_persistence_failure_leaves_delivery_for_expired_claim_recovery(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        adapter = Mock()
        adapter.run.return_value = self.output()

        def failed_write(session, **values):
            complete_job(session, **values)
            raise OperationalError('fixture', {}, Exception('fixture failure'))

        with patch(
            'app.workers.consumer.complete_job', side_effect=failed_write,
        ):
            with self.assertRaises(OperationalError):
                self.consume(delivery, lambda _: adapter)

        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, 'running')
            self.assertIsNone(job.result)

        self.expire(job_id)
        with self.sessions.begin() as session:
            self.assertEqual(fail_expired_claims(session), [job_id])

        self.sqs.change_message_visibility(
            QueueUrl=self.resources['queue_url'],
            ReceiptHandle=delivery['ReceiptHandle'],
            VisibilityTimeout=0,
        )
        messages = self.sqs.receive_message(
            QueueUrl=self.resources['queue_url'],
            WaitTimeSeconds=1,
        ).get('Messages', [])
        self.assertTrue(messages)
        factory = Mock()
        self.assertEqual(
            self.consume(messages[0], factory), 'terminal_confirmed'
        )
        factory.assert_not_called()

    def test_uncertain_visibility_prevents_scanner_start_and_ack(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        unavailable = Mock(wraps=self.sqs)
        unavailable.change_message_visibility.side_effect = (
            EndpointConnectionError(endpoint_url='http://localstack:4566')
        )
        factory = Mock()
        self.assertEqual(
            self.consume(delivery, factory, sqs=unavailable), 'interrupted'
        )
        factory.assert_not_called()
        unavailable.delete_message.assert_not_called()
        with self.sessions() as session:
            self.assertEqual(session.get(Job, job_id).status, 'running')

    def test_shutdown_interrupts_execution_without_terminal_write(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        entered = Event()

        def factory(cancel):
            adapter = Mock()

            def scan(request):
                entered.set()
                if not cancel.wait(10):
                    raise RuntimeError('Worker did not propagate shutdown')
                raise ScanInterrupted('stopped')

            adapter.run.side_effect = scan
            return adapter

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.consume, delivery, factory)
            try:
                self.assertTrue(entered.wait(5))
            finally:
                self.stopped.set()
            self.assertEqual(future.result(timeout=10), 'interrupted')

        with self.sessions() as session:
            self.assertEqual(session.get(Job, job_id).status, 'running')
        self.expire(job_id)
        with self.sessions.begin() as session:
            self.assertEqual(fail_expired_claims(session), [job_id])

    def test_recovery_preserves_cancellation_and_rejects_stale_writer(self):
        abandoned, _ = self.create()
        cancelled, _ = self.create()
        with self.sessions.begin() as session:
            old_token = claim_job(
                session, job_id=abandoned, worker_id='old', lease_seconds=3
            )
            claim_job(
                session, job_id=cancelled, worker_id='old', lease_seconds=3
            )
        self.expire(abandoned)
        self.expire(cancelled)
        with self.sessions.begin() as session:
            job = session.get(Job, cancelled)
            job.cancellation_requested_by = 'alice'
            job.cancellation_requested_at = session.scalar(
                select(func.clock_timestamp())
            )

        with self.sessions.begin() as session:
            self.assertEqual(fail_expired_claims(session), [abandoned])
        with self.sessions.begin() as session:
            self.assertFalse(complete_job(
                session,
                job_id=abandoned,
                worker_id='old',
                claim_token=old_token,
                counts=SeverityCounts(),
            ))
        with self.sessions() as session:
            self.assertEqual(session.get(Job, abandoned).status, 'failed')
            self.assertEqual(session.get(Job, cancelled).status, 'running')
            self.assertIsNotNone(
                session.get(Job, cancelled).cancellation_requested_at
            )

    def test_competing_consumers_only_launch_one_scanner(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        entered = Event()
        release = Event()
        adapter = Mock()

        def scan(request):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Test scanner was not released')
            return self.output()

        adapter.run.side_effect = scan
        factory = Mock(return_value=adapter)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(self.consume, delivery, factory, worker)
                for worker in ('worker-a', 'worker-b')
            ]
            try:
                self.assertTrue(entered.wait(5))
                deadline = time.monotonic() + 5
                while not any(future.done() for future in futures):
                    self.assertLess(time.monotonic(), deadline)
                    release.wait(0.1)
                finished = next(future for future in futures if future.done())
                self.assertEqual(finished.result(), 'not_started')
            finally:
                release.set()
            decisions = [future.result(timeout=10) for future in futures]
        self.assertEqual(sorted(decisions), ['not_started', 'processed'])
        factory.assert_called_once()
        adapter.run.assert_called_once()
        with self.sessions() as session:
            self.assertEqual(session.get(Job, job_id).attempt_count, 1)

    def test_queue_loop_processes_delivery_and_stops_before_next_poll(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        self.sqs.change_message_visibility(
            QueueUrl=self.resources['queue_url'],
            ReceiptHandle=delivery['ReceiptHandle'],
            VisibilityTimeout=0,
        )
        adapter = Mock()
        adapter.run.return_value = self.output()
        tracked = Mock(wraps=self.sqs)
        original_delete = self.sqs.delete_message

        def delete_and_stop(**values):
            response = original_delete(**values)
            self.stopped.set()
            return response

        tracked.delete_message.side_effect = delete_and_stop
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                run, self.sessions,
                sqs=tracked,
                queue_url=self.resources['queue_url'],
                worker_id='loop-worker',
                settings=self.settings,
                stopped=self.stopped,
                adapter_factory=lambda _: adapter,
                policy=self.policy,
            )
            try:
                future.result(timeout=15)
            finally:
                self.stopped.set()
        tracked.receive_message.assert_called_once()
        tracked.delete_message.assert_called_once()
        with self.sessions() as session:
            self.assertEqual(session.get(Job, job_id).status, 'completed')

    def test_periodic_renewal_failure_stops_active_scanner_without_ack(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        tracked = Mock(wraps=self.sqs)
        first = True

        def renew_visibility(**values):
            nonlocal first
            if first:
                first = False
                return self.sqs.change_message_visibility(**values)
            raise EndpointConnectionError(endpoint_url='http://localstack:4566')

        tracked.change_message_visibility.side_effect = renew_visibility
        adapter = Mock()

        def factory(cancel):
            def scan(request):
                if not cancel.wait(5):
                    raise RuntimeError('Renewal failure did not stop scanner')
                raise ScanInterrupted('authority lost')
            adapter.run.side_effect = scan
            return adapter

        self.assertEqual(
            self.consume(delivery, factory, sqs=tracked), 'interrupted'
        )
        adapter.run.assert_called_once()
        self.assertEqual(tracked.change_message_visibility.call_count, 2)
        tracked.delete_message.assert_not_called()
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, 'running')
            self.assertIsNone(job.result)

    def test_delete_failure_preserves_result_for_terminal_redelivery(self):
        job_id, body = self.create()
        delivery = self.publish(body)
        tracked = Mock(wraps=self.sqs)
        tracked.delete_message.side_effect = EndpointConnectionError(
            endpoint_url='http://localstack:4566'
        )
        adapter = Mock()
        adapter.run.return_value = self.output()
        with self.assertRaises(EndpointConnectionError):
            self.consume(delivery, lambda _: adapter, sqs=tracked)
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, 'completed')
            before = (job.result, job.finished_at, job.attempt_count)

        self.sqs.change_message_visibility(
            QueueUrl=self.resources['queue_url'],
            ReceiptHandle=delivery['ReceiptHandle'],
            VisibilityTimeout=0,
        )
        messages = self.sqs.receive_message(
            QueueUrl=self.resources['queue_url'], WaitTimeSeconds=1,
        ).get('Messages', [])
        self.assertTrue(messages)
        factory = Mock()
        self.assertEqual(
            self.consume(messages[0], factory), 'terminal_confirmed'
        )
        factory.assert_not_called()
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(
                (job.result, job.finished_at, job.attempt_count), before
            )

    def test_missing_cancel_requested_and_mismatched_jobs_never_start_or_ack(self):
        missing, missing_body = self.create()
        with self.sessions.begin() as session:
            session.delete(session.get(Job, missing))

        cancelled, cancelled_body = self.create()
        with self.sessions.begin() as session:
            job = session.get(Job, cancelled)
            job.cancellation_requested_by = 'alice'
            job.cancellation_requested_at = session.scalar(
                select(func.clock_timestamp())
            )

        job_id, body = self.create()
        adapter = Mock()
        adapter.run.return_value = self.output()
        self.consume(self.publish(body), lambda _: adapter)
        changed = json.loads(body)
        changed['parameters']['url'] += 'different'
        tracked = Mock(wraps=self.sqs)
        factory = Mock()
        for payload, expected in (
            (missing_body, 'missing_job'),
            (cancelled_body, 'not_started'),
            (json.dumps(changed), 'inconsistent_message'),
        ):
            with self.subTest(expected=expected):
                delivery = {'Body': payload, 'ReceiptHandle': 'unused'}
                self.assertEqual(
                    self.consume(delivery, factory, sqs=tracked), expected
                )
        factory.assert_not_called()
        tracked.delete_message.assert_not_called()
        tracked.change_message_visibility.assert_not_called()
        with self.sessions() as session:
            self.assertIsNone(session.get(Job, missing))
            self.assertEqual(session.get(Job, cancelled).status, 'pending')
            self.assertEqual(session.get(Job, job_id).status, 'completed')
