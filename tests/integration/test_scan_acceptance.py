"""Production submission routing with real isolated PostgreSQL storage."""
import asyncio
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_session_factory
from app.api.routes.scans import router
from app.core.auth import get_token_validator
from app.core.target_policy import TargetPolicy, get_target_policy
from app.core.team import TeamSettings, get_team_settings
from app.db.session import database_url, make_engine, make_session_factory
from app.main import app as production_app
from app.models import DispatchIntent, Job
from app.schemas.submission import ScanAcceptance
from tests.integration.test_submission_validation import post_json
from tests.submission_contract import examples
from tests.token_fixtures import TokenFixtures


class SubmissionOpenAPITests(unittest.TestCase):
    def test_production_route_documents_acceptance(self):
        operation = production_app.openapi()['paths']['/scan']['post']
        self.assertIn('202', operation['responses'])
        self.assertIn('422', operation['responses'])
        self.assertTrue(operation['security'])
        schema = production_app.openapi()['components']['schemas'][
            'ScanAcceptance'
        ]
        self.assertEqual(
            set(schema['required']),
            {'job_id', 'scan_type', 'status'},
        )
        self.assertEqual(
            schema['properties']['job_id']['format'], 'uuid',
        )


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class ScanAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    def setUp(self):
        self.schema = 'test_acceptance_' + uuid4().hex
        self.admin = make_engine()
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.drop_schema)

        self.engine = self.new_engine()
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        config = Config('alembic.ini')
        with self.engine.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')

        self.resolver = Mock(return_value=('8.8.8.8',))
        self.policy = TargetPolicy(
            ['https://approved.example.invalid'], self.resolver,
        )
        self.app = production_app
        previous = dict(self.app.dependency_overrides)
        self.addCleanup(self.restore_overrides, previous)
        self.configure(self.app, self.sessions)

    def new_engine(self):
        return create_engine(
            database_url(),
            connect_args={
                'options': (
                    f'-c search_path={self.schema} -c timezone=UTC'
                ),
            },
        )

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA "{self.schema}" CASCADE'
            )

    def restore_overrides(self, previous):
        self.app.dependency_overrides.clear()
        self.app.dependency_overrides.update(previous)

    def configure(self, app, sessions):
        app.dependency_overrides[get_session_factory] = lambda: sessions
        app.dependency_overrides[get_token_validator] = (
            lambda: self.tokens.validator()
        )
        app.dependency_overrides[get_team_settings] = (
            lambda: TeamSettings()
        )
        app.dependency_overrides[get_target_policy] = lambda: self.policy

    def payload(self):
        return {
            'scan_type': 'zap',
            'parameters': {'url': 'https://approved.example.invalid/'},
        }

    def post(self, payload=None, authorization='valid', headers=(), app=None):
        if authorization == 'valid':
            authorization = 'Bearer ' + self.tokens.sign()
        return asyncio.run(post_json(
            self.app if app is None else app,
            self.payload() if payload is None else payload,
            authorization,
            headers,
        ))

    def counts(self):
        with self.sessions() as session:
            return (
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(
                    select(func.count()).select_from(DispatchIntent)
                ),
            )

    def test_202_contains_committed_job_and_unpublished_intent(self):
        status, body = self.post()
        accepted = json.loads(body)
        self.assertEqual(status, 202)
        self.assertEqual(
            set(accepted), {'job_id', 'scan_type', 'status'},
        )
        self.assertEqual(accepted['scan_type'], 'zap')
        self.assertEqual(accepted['status'], 'pending')
        job_id = UUID(accepted['job_id'])
        self.assertEqual(self.counts(), (1, 1))
        with self.sessions() as session:
            job = session.get(Job, job_id)
            intent = session.scalar(
                select(DispatchIntent).where(
                    DispatchIntent.job_id == job_id,
                )
            )
            self.assertEqual(job.team_id, 'team-alpha')
            self.assertEqual(job.created_by, 'fixture-subject')
            self.assertEqual(job.parameters, self.payload()['parameters'])
            self.assertEqual(job.status, 'pending')
            self.assertIsNone(job.started_at)
            self.assertIsNone(intent.published_at)

    def test_denied_requests_create_no_records(self):
        cases = (
            (None, (), 401),
            ('Bearer malformed', (), 401),
            (
                'Bearer ' + self.tokens.sign(
                    self.tokens.claims(team_ids=[]),
                ),
                (),
                403,
            ),
            ('valid', ((b'x-team-id', b'team-beta'),), 403),
        )
        for authorization, headers, expected in cases:
            with self.subTest(expected=expected, authorization=authorization):
                status, _ = self.post(
                    authorization=authorization, headers=headers,
                )
                self.assertEqual(status, expected)
                self.assertEqual(self.counts(), (0, 0))
        self.resolver.assert_not_called()

        self.resolver.return_value = ('127.0.0.1',)
        status, _ = self.post()
        self.assertEqual(status, 403)
        self.assertEqual(self.counts(), (0, 0))

    def test_invalid_submission_returns_422_without_records(self):
        for example in examples()['invalid']:
            with self.subTest(example=example['name']):
                status, body = self.post(payload=example['payload'])
                self.assertEqual(status, 422)
                self.assertIsInstance(json.loads(body)['detail'], list)
                self.assertEqual(self.counts(), (0, 0))
        self.resolver.assert_not_called()

    def test_commit_failure_returns_safe_503_and_rolls_back(self):
        def fail_commit(session):
            raise OperationalError(
                'sensitive SQL', {}, Exception('sensitive connection detail'),
            )

        event.listen(
            self.sessions.class_, 'before_commit', fail_commit,
        )
        try:
            status, body = self.post()
        finally:
            event.remove(
                self.sessions.class_, 'before_commit', fail_commit,
            )
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {'detail': 'Durable acceptance could not be confirmed'},
        )
        self.assertEqual(self.counts(), (0, 0))

    def test_acceptance_survives_application_and_pool_replacement(self):
        status, body = self.post()
        self.assertEqual(status, 202)
        first_id = UUID(json.loads(body)['job_id'])
        self.engine.dispose()

        engine = self.new_engine()
        self.addCleanup(engine.dispose)
        sessions = make_session_factory(engine)
        replacement = FastAPI()
        replacement.include_router(router)
        self.configure(replacement, sessions)

        with sessions() as session:
            self.assertIsNotNone(session.get(Job, first_id))
            self.assertIsNotNone(session.scalar(
                select(DispatchIntent).where(
                    DispatchIntent.job_id == first_id,
                )
            ))

        status, body = self.post(app=replacement)
        self.assertEqual(status, 202)
        self.assertNotEqual(UUID(json.loads(body)['job_id']), first_id)
        self.assertEqual(self.counts(), (2, 2))

    def test_response_is_sent_only_after_commit(self):
        committed = []

        def record_commit(session):
            committed.append(True)

        event.listen(
            self.sessions.class_, 'after_commit', record_commit,
        )
        original_validate = ScanAcceptance.model_validate

        def validate_after_commit(value):
            self.assertEqual(committed, [True])
            self.assertEqual(self.counts(), (1, 1))
            return original_validate(value)

        try:
            with patch(
                'app.api.routes.scans.ScanAcceptance.model_validate',
                side_effect=validate_after_commit,
            ):
                status, _ = self.post()
        finally:
            event.remove(
                self.sessions.class_, 'after_commit', record_commit,
            )
        self.assertEqual(status, 202)
