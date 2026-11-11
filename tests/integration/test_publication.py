"""Publication against isolated PostgreSQL and optional LocalStack."""
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import boto3
from alembic import command
from alembic.config import Config
from botocore.config import Config as AWSConfig
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.team import TeamContext
from app.db.session import database_url, make_engine, make_session_factory
from app.dispatchers.main import run_pass
from app.dispatchers.publication import MAX_ATTEMPTS, publish_job
from app.models import DispatchIntent, Job
from app.schemas import ZapSubmission
from app.services.acceptance import accept_scan
from docker.localstack.provision import provision


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for publication storage tests',
)
class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_publication_' + uuid4().hex
        self.admin = make_engine()
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.drop_schema)
        self.engine = create_engine(database_url(), connect_args={
            'options': f'-c search_path={self.schema} -c timezone=UTC',
        })
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        config = Config('alembic.ini')
        with self.engine.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')

        fixture = (
            Path(__file__).resolve().parents[1]
            / 'fixtures/contracts/v1/job-message.json'
        )
        self.payload = json.loads(fixture.read_text())
        submission = ZapSubmission.model_validate({
            'scan_type': self.payload['scan_type'],
            'parameters': self.payload['parameters'],
        })
        accepted = accept_scan(
            self.sessions, submission=submission,
            context=TeamContext('team-alpha', 'alice'),
        )
        self.job_id = accepted.job_id
        self.payload['job_id'] = str(self.job_id)
        self.sns = Mock()
        self.sns.publish.return_value = {'MessageId': 'fixture-message'}

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')

    def publish(self, **changes):
        values = {
            'job_id': self.job_id, 'sns': self.sns, 'topic_arn': 'fixture-topic',
        }
        values.update(changes)
        return publish_job(self.sessions, **values)

    def intent(self):
        with self.sessions() as session:
            return session.scalar(select(DispatchIntent).where(
                DispatchIntent.job_id == self.job_id,
            ))

    def change_intent(self, **values):
        with self.sessions.begin() as session:
            session.execute(
                update(DispatchIntent)
                .where(DispatchIntent.job_id == self.job_id)
                .values(**values)
            )

    def make_due(self, **values):
        self.change_intent(
            next_attempt_at=(
                func.clock_timestamp() - timedelta(seconds=1)
            ),
            **values,
        )

    def delete_queue(self, sqs, name):
        try:
            queue = sqs.get_queue_url(QueueName=name)['QueueUrl']
        except ClientError as error:
            if error.response['Error']['Code'] in (
                'AWS.SimpleQueueService.NonExistentQueue', 'QueueDoesNotExist',
            ):
                return
            raise
        sqs.delete_queue(QueueUrl=queue)

    def test_success_uses_saved_contract_and_records_publication(self):
        with self.assertLogs('app.dispatchers.publication', level='INFO') as logs:
            self.assertEqual(self.publish(), 'published')
        request = self.sns.publish.call_args.kwargs
        self.assertEqual(json.loads(request['Message']), self.payload)
        self.assertEqual(request['TopicArn'], 'fixture-topic')
        self.assertEqual(request['MessageAttributes'], {
            'scan_type': {'DataType': 'String', 'StringValue': 'zap'},
        })
        self.assertEqual(logs.records[-1].job_id, str(self.job_id))
        intent = self.intent()
        self.assertEqual(intent.attempt_count, 1)
        self.assertIsNotNone(intent.published_at)
        self.assertEqual(intent.published_at.utcoffset().total_seconds(), 0)
        self.assertIsNone(intent.claim_token)
        self.assertIsNone(intent.lease_expires_at)
        self.assertIsNone(intent.last_error_code)
        with self.sessions() as session:
            job = session.get(Job, self.job_id)
            self.assertEqual(job.status, 'pending')
            self.assertEqual(job.parameters, self.payload['parameters'])
        self.assertEqual(self.publish(), 'skipped')
        self.sns.publish.assert_called_once()

    def test_failure_preserves_intent_and_records_safe_error(self):
        self.sns.publish.side_effect = EndpointConnectionError(
            endpoint_url='https://sensitive.example.invalid/',
        )
        self.assertEqual(self.publish(), 'failed')
        intent = self.intent()
        self.assertIsNone(intent.published_at)
        self.assertIsNone(intent.claim_token)
        self.assertEqual(intent.attempt_count, 1)
        self.assertEqual(intent.last_error_code, 'sns_publication_failed')
        with self.sessions() as session:
            self.assertEqual(session.get(Job, self.job_id).status, 'pending')

    def test_missing_acknowledgement_is_not_success(self):
        self.sns.publish.return_value = {}
        self.assertEqual(self.publish(), 'failed')
        self.assertIsNone(self.intent().published_at)
        self.assertEqual(
            self.intent().last_error_code, 'sns_acknowledgement_missing',
        )

    def test_unsupported_saved_version_is_not_sent(self):
        with self.sessions.begin() as session:
            session.execute(update(DispatchIntent).where(
                DispatchIntent.job_id == self.job_id,
            ).values(schema_version=2))
        self.assertEqual(self.publish(), 'invalid')
        self.sns.publish.assert_not_called()
        self.assertEqual(self.intent().last_error_code, 'invalid_job_message')
        self.assertIsNone(self.intent().published_at)

    def test_committed_claim_blocks_competing_publication(self):
        observed = []

        def publish(**request):
            self.assertIsNotNone(self.intent().claim_token)
            competitor = Mock()
            observed.append(self.publish(sns=competitor))
            competitor.publish.assert_not_called()
            return {'MessageId': 'fixture-message'}

        self.sns.publish.side_effect = publish
        self.assertEqual(self.publish(), 'published')
        self.assertEqual(observed, ['skipped'])

    def test_stale_attempt_cannot_overwrite_replacement_claim(self):
        replacement = uuid4()

        def publish(**request):
            with self.sessions.begin() as session:
                session.execute(update(DispatchIntent).where(
                    DispatchIntent.job_id == self.job_id,
                ).values(claim_token=replacement))
            return {'MessageId': 'fixture-message'}

        self.sns.publish.side_effect = publish
        self.assertEqual(self.publish(), 'claim_lost')
        self.assertEqual(self.intent().claim_token, replacement)
        self.assertIsNone(self.intent().published_at)

    def test_ineligible_and_missing_intents_are_skipped(self):
        self.assertEqual(self.publish(job_id=uuid4()), 'skipped')
        with self.sessions.begin() as session:
            intent = session.scalar(select(DispatchIntent).where(
                DispatchIntent.job_id == self.job_id,
            ))
            intent.suppressed_at = intent.created_at
        self.assertEqual(self.publish(), 'skipped')
        self.sns.publish.assert_not_called()
        self.assertEqual(self.intent().attempt_count, 0)

    def test_dispatcher_discovers_committed_job(self):
        self.assertEqual(run_pass(
            self.sessions, sns=self.sns,
            topic_arn='fixture-topic', stopped=Event(),
        ), ['published'])
        self.assertIsNotNone(self.intent().published_at)

    def test_dispatcher_waits_then_recovers_saved_failure(self):
        self.sns.publish.side_effect = EndpointConnectionError(
            endpoint_url='http://unavailable.invalid/',
        )
        options = {
            'sns': self.sns,
            'topic_arn': 'fixture-topic',
            'stopped': Event(),
        }
        with self.assertLogs(
            'app.dispatchers.publication', level='INFO',
        ) as logs:
            self.assertEqual(
                run_pass(self.sessions, **options), ['failed'],
            )

        intent = self.intent()
        with self.sessions() as session:
            now = session.scalar(select(func.clock_timestamp()))
        self.assertGreater(intent.next_attempt_at, now)
        self.assertEqual(logs.records[-1].attempt_count, 1)
        self.assertEqual(
            logs.records[-1].last_error_code,
            'sns_publication_failed',
        )
        self.assertNotEqual(
            logs.records[-1].next_attempt_at, '-',
        )
        self.assertEqual(run_pass(self.sessions, **options), [])
        self.assertEqual(self.publish(), 'skipped')
        self.sns.publish.assert_called_once()

        self.make_due()
        self.assertEqual(
            self.publish(first_attempt_only=True), 'skipped',
        )
        self.sns.publish.side_effect = None
        self.engine.dispose()

        self.assertEqual(
            run_pass(self.sessions, **options), ['published'],
        )
        self.assertEqual(self.intent().attempt_count, 2)
        self.assertIsNotNone(self.intent().published_at)
        self.assertIsNone(self.intent().last_error_code)
        with self.sessions() as session:
            job = session.get(Job, self.job_id)
            self.assertEqual(job.status, 'pending')
            self.assertEqual(job.parameters, self.payload['parameters'])

    def test_retry_budget_preserves_exhausted_intent(self):
        self.sns.publish.side_effect = EndpointConnectionError(
            endpoint_url='http://unavailable.invalid/',
        )
        options = {
            'sns': self.sns,
            'topic_arn': 'fixture-topic',
            'stopped': Event(),
        }
        with self.assertLogs(
            'app.dispatchers.publication', level='INFO',
        ) as logs:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                self.make_due()
                expected = (
                    'exhausted'
                    if attempt == MAX_ATTEMPTS else 'failed'
                )
                self.assertEqual(
                    run_pass(self.sessions, **options), [expected],
                )
                self.assertEqual(
                    self.intent().attempt_count, attempt,
                )

        self.assertEqual(
            logs.records[-1].dispatch_outcome, 'exhausted',
        )
        self.assertEqual(
            logs.records[-1].attempt_count, MAX_ATTEMPTS,
        )
        self.make_due()
        self.assertEqual(run_pass(self.sessions, **options), [])
        self.assertEqual(self.publish(), 'skipped')
        self.assertEqual(self.sns.publish.call_count, MAX_ATTEMPTS)
        intent = self.intent()
        self.assertIsNone(intent.published_at)
        self.assertIsNone(intent.suppressed_at)
        self.assertIsNone(intent.claim_token)
        self.assertEqual(
            intent.last_error_code, 'sns_publication_failed',
        )
        with self.sessions() as session:
            self.assertEqual(
                session.get(Job, self.job_id).status, 'pending',
            )

    def test_active_claim_waits_and_expired_claim_recovers(self):
        self.make_due(
            attempt_count=1,
            claim_token=uuid4(),
            lease_expires_at=(
                func.clock_timestamp() + timedelta(seconds=60)
            ),
        )
        options = {
            'sns': self.sns,
            'topic_arn': 'fixture-topic',
            'stopped': Event(),
        }
        self.assertEqual(run_pass(self.sessions, **options), [])
        self.assertEqual(self.publish(), 'skipped')
        self.sns.publish.assert_not_called()

        self.make_due(
            lease_expires_at=(
                func.clock_timestamp() - timedelta(seconds=1)
            ),
        )
        self.assertEqual(
            run_pass(self.sessions, **options), ['published'],
        )
        self.assertEqual(self.intent().attempt_count, 2)
        self.assertIsNone(self.intent().claim_token)

    def test_interrupted_final_attempt_cannot_exceed_budget(self):
        self.make_due(
            attempt_count=MAX_ATTEMPTS,
            claim_token=uuid4(),
            lease_expires_at=(
                func.clock_timestamp() - timedelta(seconds=1)
            ),
        )
        self.assertEqual(run_pass(
            self.sessions,
            sns=self.sns,
            topic_arn='fixture-topic',
            stopped=Event(),
        ), [])
        self.assertEqual(self.publish(), 'skipped')
        self.sns.publish.assert_not_called()
        self.assertIsNone(self.intent().published_at)
        self.assertEqual(
            self.intent().attempt_count, MAX_ATTEMPTS,
        )

    def test_invalid_message_is_not_retried_automatically(self):
        self.change_intent(schema_version=2)
        self.assertEqual(run_pass(
            self.sessions,
            sns=self.sns,
            topic_arn='fixture-topic',
            stopped=Event(),
        ), ['invalid'])
        self.make_due()
        self.assertEqual(run_pass(
            self.sessions,
            sns=self.sns,
            topic_arn='fixture-topic',
            stopped=Event(),
        ), [])
        self.assertEqual(self.publish(), 'skipped')
        self.assertEqual(self.intent().attempt_count, 1)
        self.sns.publish.assert_not_called()

    def test_finalization_failure_allows_duplicate_publication(self):
        @contextmanager
        def fail_after_publish():
            with self.sessions.begin() as session:
                yield session
                if self.sns.publish.called:
                    raise SQLAlchemyError(
                        'Injected finalization failure',
                    )

        broken_sessions = Mock()
        broken_sessions.begin.side_effect = fail_after_publish

        with self.assertRaises(SQLAlchemyError):
            publish_job(
                broken_sessions,
                job_id=self.job_id,
                sns=self.sns,
                topic_arn='fixture-topic',
            )

        intent = self.intent()
        self.assertIsNone(intent.published_at)
        self.assertIsNotNone(intent.claim_token)
        self.assertEqual(intent.attempt_count, 1)
        first_request = self.sns.publish.call_args.kwargs

        self.make_due(
            lease_expires_at=(
                func.clock_timestamp() - timedelta(seconds=1)
            ),
        )
        self.assertEqual(run_pass(
            self.sessions,
            sns=self.sns,
            topic_arn='fixture-topic',
            stopped=Event(),
        ), ['published'])
        self.assertEqual(self.sns.publish.call_count, 2)
        self.assertEqual(
            self.sns.publish.call_args.kwargs, first_request,
        )
        self.assertEqual(self.intent().attempt_count, 2)
        self.assertIsNotNone(self.intent().published_at)

    @unittest.skipUnless(
        os.environ.get('RUN_LOCALSTACK_TESTS') == '1',
        'Set RUN_LOCALSTACK_TESTS=1 for real publication routing',
    )
    def test_saved_job_reaches_queue_after_sns_access_recovers(self):
        endpoint = 'http://localstack:4566'
        if any(os.environ.get(name) != endpoint for name in (
            'SNS_ENDPOINT_URL', 'SQS_ENDPOINT_URL',
        )):
            raise RuntimeError('Default LocalStack endpoints required')
        options = {
            'endpoint_url': endpoint,
            'region_name': 'us-east-1',
            'aws_access_key_id': 'test',
            'aws_secret_access_key': 'test',
            'config': AWSConfig(
                connect_timeout=3, read_timeout=5,
                retries={'total_max_attempts': 1},
            ),
        }
        sns = boto3.client('sns', **options)
        sqs = boto3.client('sqs', **options)
        self.addCleanup(sns.close)
        self.addCleanup(sqs.close)
        prefix = 'test-publication-' + uuid4().hex
        topic = sns.create_topic(Name=prefix)['TopicArn']
        self.addCleanup(sns.delete_topic, TopicArn=topic)
        self.addCleanup(self.delete_queue, sqs, prefix + '-dlq')
        self.addCleanup(self.delete_queue, sqs, prefix + '-zap')
        resources = provision(
            sns, sqs, topic_name=prefix,
            queue_name=prefix + '-zap', dlq_name=prefix + '-dlq',
        )
        queue = resources['queue_url']
        # Exercise the actual process entry point, injecting only its database
        # engine factory so discovery cannot touch real application jobs.
        script = '''
import os
from sqlalchemy import create_engine
from app.db.session import database_url
from app.dispatchers import main
schema = os.environ['TEST_PUBLICATION_SCHEMA']
main.make_engine = lambda: create_engine(database_url(), connect_args={
    'options': f'-c search_path={schema} -c timezone=UTC',
})
main.main()
'''
        def start_process(sns_endpoint):
            return subprocess.Popen(
                [sys.executable, '-B', '-c', script],
                env={
                    **os.environ,
                    'SNS_TOPIC_ARN': resources['topic_arn'],
                    'SNS_ENDPOINT_URL': sns_endpoint,
                    'TEST_PUBLICATION_SCHEMA': self.schema,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        def stop_process(process):
            process.terminate()
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
                self.fail('Dispatcher did not stop after SIGTERM')
            self.assertEqual(process.returncode, 0, output)
            self.assertFalse(
                Path('/tmp/dispatcher-ready').exists(),
            )
            return output

        # Refuse SNS access only for this isolated dispatcher process.
        process = start_process('http://127.0.0.1:1')
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.intent().last_error_code is not None:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            intent = self.intent()
            self.assertEqual(
                intent.last_error_code, 'sns_publication_failed',
            )
            self.assertIsNone(intent.published_at)
            self.assertGreaterEqual(intent.attempt_count, 1)
            self.assertLess(intent.attempt_count, MAX_ATTEMPTS)
        finally:
            failed_output = stop_process(process)

        self.assertIn(
            f'job_id={self.job_id} outcome=failed',
            failed_output,
        )
        self.engine.dispose()

        # A new process restores real LocalStack SNS access.
        process = start_process(endpoint)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.intent().published_at is not None:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(self.intent().published_at)
            self.assertGreaterEqual(self.intent().attempt_count, 2)
            self.assertTrue(
                Path('/tmp/dispatcher-ready').exists(),
            )
        finally:
            output = stop_process(process)

        self.assertIn('dispatcher_started', output)
        self.assertIn(
            f'job_id={self.job_id} outcome=published',
            output,
        )
        received = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not received:
            received = sqs.receive_message(
                QueueUrl=queue, WaitTimeSeconds=1, MaxNumberOfMessages=10,
            ).get('Messages', [])
        self.assertTrue(received, 'Published job did not reach ZAP queue')
        for message in received:
            self.assertEqual(json.loads(message['Body']), self.payload)
        self.assertIsNotNone(self.intent().published_at)
