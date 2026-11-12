"""Partial local integration slice; no real scanner or target connection."""
import asyncio
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import Mock
from uuid import UUID, uuid4

import boto3
from alembic import command
from alembic.config import Config
from botocore.config import Config as AWSConfig
from botocore.exceptions import ClientError, EndpointConnectionError
from sqlalchemy import create_engine, func, select

from app.api.dependencies import get_session_factory
from app.core.auth import OIDCSettings, get_token_validator
from app.core.failure import safe_error
from app.core.target_policy import TargetPolicy, get_target_policy
from app.db.session import database_url, make_engine, make_session_factory
from app.dispatchers.publication import publish_job
from app.main import app
from app.models import DispatchIntent, Job
from app.repositories.lifecycle import complete_job, fail_job
from app.scanners.base import ScannerOutput
from app.scanners.zap_outcomes import execute_zap_scan
from app.workers.zap import handle_message
from docker.localstack.provision import provision
from tests.auth_support import get
from tests.integration.test_submission_validation import post_json
from tests.submission_contract import examples


ENABLED = all(
    os.environ.get(name) == '1'
    for name in (
        'RUN_LOCAL_SLICE_TESTS',
        'RUN_POSTGRES_TESTS',
        'RUN_LOCALSTACK_TESTS',
        'RUN_KEYCLOAK_TESTS',
    )
)


@unittest.skipUnless(
    ENABLED,
    'Use scripts/verify_local_slice.py for this opt-in local slice',
)
class LocalSliceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = OIDCSettings.from_environment()
        expected = {
            'PGHOST': 'postgres',
            'SNS_ENDPOINT_URL': 'http://localstack:4566',
            'SQS_ENDPOINT_URL': 'http://localstack:4566',
            'OIDC_TEAM_CLAIM': 'team_ids',
        }
        if any(os.environ.get(key) != value
               for key, value in expected.items()):
            raise RuntimeError('Local slice requires default local services')
        if (
            settings.issuer
            != 'http://localhost:8080/realms/elasticscan'
            or settings.jwks_url
            != (
                'http://keycloak:8080/realms/elasticscan/'
                'protocol/openid-connect/certs'
            )
        ):
            raise RuntimeError('Local slice requires the local Keycloak realm')

        get_token_validator.cache_clear()
        cls.addClassCleanup(get_token_validator.cache_clear)
        cls.tokens = {}
        cls.subjects = {}
        for user in ('alice', 'alice-peer', 'bob'):
            data = urlencode({
                'grant_type': 'password',
                'client_id': 'elasticscan-dev',
                'username': user,
                'password': os.environ['LOCAL_USER_PASSWORD'],
            }).encode()
            request = Request(
                'http://keycloak:8080/realms/elasticscan/'
                'protocol/openid-connect/token',
                data=data,
            )
            with urlopen(request, timeout=10) as response:
                token = json.load(response)['access_token']
            claims = get_token_validator().validate(token)
            team = 'team-beta' if user == 'bob' else 'team-alpha'
            if claims.get('team_ids') != [team]:
                raise RuntimeError('Unexpected development team membership')
            cls.tokens[user] = token
            cls.subjects[user] = claims['sub']

    def setUp(self):
        self.schema = 'test_local_slice_' + uuid4().hex
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
            self.assertTrue(connection.exec_driver_sql(
                'SELECT version_num FROM alembic_version'
            ).scalar())

        self.policy = TargetPolicy(
            ['https://approved.example.invalid'],
            resolver=lambda *_: ('8.8.8.8',),
        )
        previous = dict(app.dependency_overrides)
        self.addCleanup(self.restore_overrides, previous)
        app.dependency_overrides[get_session_factory] = lambda: self.sessions
        app.dependency_overrides[get_target_policy] = lambda: self.policy

        options = {
            'endpoint_url': 'http://localstack:4566',
            'region_name': 'us-east-1',
            'aws_access_key_id': 'test',
            'aws_secret_access_key': 'test',
            'config': AWSConfig(
                connect_timeout=3,
                read_timeout=5,
                retries={'max_attempts': 1},
            ),
        }
        self.sns = boto3.client('sns', **options)
        self.sqs = boto3.client('sqs', **options)
        self.addCleanup(self.sns.close)
        self.addCleanup(self.sqs.close)

        prefix = 'test-local-slice-' + uuid4().hex
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
        self.owners = {}

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA "{self.schema}" CASCADE'
            )

    def restore_overrides(self, previous):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

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

    def payload(self):
        return {
            'scan_type': 'zap',
            'parameters': {'url': 'https://approved.example.invalid/'},
        }

    def authorization(self, user):
        return None if user is None else 'Bearer ' + self.tokens[user]

    def post(self, payload=None, user='alice', headers=()):
        status, raw = asyncio.run(post_json(
            app,
            self.payload() if payload is None else payload,
            self.authorization(user),
            headers,
        ))
        return status, json.loads(raw)

    def read(self, job_id, user='alice'):
        status, _, raw = asyncio.run(get(
            app,
            self.authorization(user),
            path=f'/scan/{job_id}',
        ))
        return status, json.loads(raw)

    def submit(self, user='alice'):
        status, body = self.post(user=user)
        self.assertEqual(status, 202)
        self.assertEqual(set(body), {'job_id', 'scan_type', 'status'})
        self.assertEqual(body['scan_type'], 'zap')
        self.assertEqual(body['status'], 'pending')
        job_id = UUID(body['job_id'])
        self.owners[job_id] = user
        self.assert_state(job_id, 'pending')
        return job_id

    def assert_state(self, job_id, state):
        owner = self.owners[job_id]
        status, body = self.read(job_id, owner)
        self.assertEqual(status, 200)
        self.assertEqual(body['job_id'], str(job_id))
        self.assertEqual(body['status'], state)

        other = 'alice' if owner == 'bob' else 'bob'
        self.assertEqual(
            self.read(job_id, other),
            self.read(uuid4(), other),
        )
        self.assertEqual(self.read(job_id, other)[0], 404)
        if owner == 'alice':
            self.assertEqual(self.read(job_id, 'alice-peer'), (status, body))

        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.created_by, self.subjects[owner])
            self.assertEqual(
                job.team_id,
                'team-beta' if owner == 'bob' else 'team-alpha',
            )
        for name in ('created_at', 'updated_at', 'started_at', 'finished_at'):
            if body[name] is not None:
                value = datetime.fromisoformat(
                    body[name].replace('Z', '+00:00')
                )
                self.assertEqual(value.utcoffset(), timedelta(0))
        return body

    def counts(self):
        with self.sessions() as session:
            return (
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(
                    select(func.count()).select_from(DispatchIntent)
                ),
            )

    def receive(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            messages = self.sqs.receive_message(
                QueueUrl=self.resources['queue_url'],
                MaxNumberOfMessages=1,
                WaitTimeSeconds=1,
            ).get('Messages', [])
            if messages:
                self.assertEqual(
                    json.loads(messages[0]['Body'])['job_id'],
                    str(job_id),
                )
                return messages[0]
        self.fail('Published job did not reach its isolated queue')

    def publish(self, job_id):
        self.assertEqual(publish_job(
            self.sessions,
            job_id=job_id,
            sns=self.sns,
            topic_arn=self.resources['topic_arn'],
        ), 'published')
        with self.sessions() as session:
            intent = session.scalar(select(DispatchIntent).where(
                DispatchIntent.job_id == job_id
            ))
            self.assertIsNotNone(intent.published_at)
        return self.receive(job_id)

    def scanner(self):
        scanner = Mock()
        scanner.run.return_value = ScannerOutput(
            return_code=2,
            report=json.dumps({
                'site': [{
                    'alerts': [
                        {'riskcode': risk} for risk in ('3', '2', '1')
                    ],
                }],
            }),
            passive_complete=True,
            active_scan_count=0,
            visited_urls=('https://approved.example.invalid/',),
        )
        return scanner

    def handle(self, body, adapter):
        return handle_message(
            body,
            sessions=self.sessions,
            worker_id='local-slice-worker',
            adapter=adapter,
            lease_seconds=300,
            policy=self.policy,
        )

    def process(self, job_id, message, scanner):
        observed = {}
        bridge = Mock()

        def run(request):
            self.assertEqual(request.job_id, job_id)
            self.assert_state(job_id, 'running')
            observed['request'] = request
            observed['outcome'] = execute_zap_scan(scanner, request)
            return None

        bridge.run.side_effect = run
        self.assertEqual(self.handle(message['Body'], bridge), 'invoked')
        bridge.run.assert_called_once()
        scanner.run.assert_called_once()
        request = observed['request']
        outcome = observed['outcome']

        # Test orchestration only; production worker wiring is unchanged.
        with self.sessions.begin() as session:
            common = {
                'job_id': job_id,
                'worker_id': 'local-slice-worker',
                'claim_token': request.claim_token,
            }
            if outcome.status == 'completed':
                changed = complete_job(
                    session, counts=outcome.counts, **common
                )
            else:
                changed = fail_job(session, outcome=outcome, **common)
            self.assertTrue(changed)

        body = self.assert_state(job_id, outcome.status)
        self.sqs.delete_message(
            QueueUrl=self.resources['queue_url'],
            ReceiptHandle=message['ReceiptHandle'],
        )
        return body

    def test_success_and_terminal_redelivery_for_two_teams(self):
        for user in ('alice', 'bob'):
            with self.subTest(user=user):
                job_id = self.submit(user)
                message = self.publish(job_id)
                scanner = self.scanner()
                body = self.process(job_id, message, scanner)
                self.assertEqual(
                    body['result'], {'high': 1, 'medium': 1, 'low': 1}
                )
                self.assertIsNone(body['error'])
                self.assertIsNotNone(body['started_at'])
                self.assertIsNotNone(body['finished_at'])

                duplicate = Mock()
                self.assertEqual(
                    self.handle(message['Body'], duplicate), 'not_started'
                )
                duplicate.run.assert_not_called()
                self.assertEqual(
                    self.assert_state(job_id, 'completed'), body
                )

    def test_simulated_timeout_is_stored_as_safe_failure(self):
        job_id = self.submit()
        message = self.publish(job_id)
        scanner = Mock()
        scanner.run.side_effect = TimeoutError('password=secret')
        body = self.process(job_id, message, scanner)
        self.assertIsNone(body['result'])
        self.assertEqual(body['error'], safe_error('scanner_timeout'))
        self.assertNotIn('password', json.dumps(body))

    def test_rejected_requests_create_no_jobs_or_intents(self):
        self.assertEqual(self.post(user=None)[0], 401)
        status, _, _ = asyncio.run(get(
            app, 'Bearer malformed', path=f'/scan/{uuid4()}'
        ))
        self.assertEqual(status, 401)
        self.assertEqual(self.post(
            headers=((b'x-team-id', b'team-beta'),)
        )[0], 403)
        for example in examples()['invalid']:
            with self.subTest(example=example['name']):
                self.assertEqual(self.post(example['payload'])[0], 422)
        self.assertEqual(self.post({
            'scan_type': 'zap',
            'parameters': {'url': 'http://127.0.0.1/'},
        })[0], 403)
        self.assertEqual(self.counts(), (0, 0))

    def test_unrelated_routing_attribute_does_not_reach_zap_queue(self):
        job_id = self.submit()
        body = json.dumps({
            'schema_version': 1,
            'job_id': str(job_id),
            **self.payload(),
        })
        self.sns.publish(
            TopicArn=self.resources['topic_arn'],
            Message=body,
            MessageAttributes={
                'scan_type': {
                    'DataType': 'String', 'StringValue': 'unrelated',
                },
            },
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            messages = self.sqs.receive_message(
                QueueUrl=self.resources['queue_url'],
                WaitTimeSeconds=1,
            ).get('Messages', [])
            self.assertEqual(messages, [])
        self.assert_state(job_id, 'pending')
        message = self.publish(job_id)
        self.process(job_id, message, self.scanner())

    def test_publication_failure_preserves_job_then_recovers(self):
        job_id = self.submit()
        unavailable = Mock()
        unavailable.publish.side_effect = EndpointConnectionError(
            endpoint_url='http://localstack:4566'
        )
        self.assertEqual(publish_job(
            self.sessions,
            job_id=job_id,
            sns=unavailable,
            topic_arn=self.resources['topic_arn'],
        ), 'failed')
        self.assert_state(job_id, 'pending')
        with self.sessions.begin() as session:
            intent = session.scalar(select(DispatchIntent).where(
                DispatchIntent.job_id == job_id
            ))
            self.assertIsNone(intent.published_at)
            self.assertEqual(intent.last_error_code, 'sns_publication_failed')
            intent.next_attempt_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        message = self.publish(job_id)
        self.process(job_id, message, self.scanner())

    def test_invalid_deliveries_never_invoke_scanner(self):
        job_id = self.submit()
        inconsistent = {
            'schema_version': 1,
            'job_id': str(job_id),
            'scan_type': 'zap',
            'parameters': {
                'url': 'https://approved.example.invalid/different',
            },
        }
        adapter = Mock()
        for body, expected in (
            ('{', 'invalid_message'),
            ('{"schema_version": 99}', 'invalid_message'),
            (json.dumps(inconsistent), 'inconsistent_message'),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.handle(body, adapter), expected)
                self.assert_state(job_id, 'pending')
        adapter.run.assert_not_called()
        self.assertEqual(self.counts(), (1, 1))


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LocalSliceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(
        0 if result.wasSuccessful()
        and not result.skipped
        and result.testsRun == 6
        else 1
    )
