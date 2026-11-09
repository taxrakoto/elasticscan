"""Production team-scoped retrieval with isolated PostgreSQL storage."""
import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError

from app.api.dependencies import get_session_factory
from app.core.auth import get_token_validator
from app.core.target_policy import TargetPolicy, get_target_policy
from app.core.team import TeamContext, TeamSettings, get_team_settings
from app.db.session import database_url, make_engine, make_session_factory
from app.main import app
from app.models import DispatchIntent, Job
from app.schemas import ZapSubmission
from app.services.acceptance import accept_scan
from tests.auth_support import get
from tests.integration.test_submission_validation import post_json
from tests.token_fixtures import TokenFixtures


class RetrievalOpenAPITests(unittest.TestCase):
    def test_route_documents_uuid_security_and_responses(self):
        document = app.openapi()
        operation = document['paths']['/scan/{job_id}']['get']
        self.assertTrue(operation['security'])
        for status in ('200', '401', '403', '404', '422', '503'):
            self.assertIn(status, operation['responses'])
        parameter = next(
            item for item in operation['parameters']
            if item['name'] == 'job_id'
        )
        self.assertEqual(parameter['in'], 'path')
        self.assertEqual(parameter['schema']['format'], 'uuid')
        schema = document['components']['schemas']['JobStatusResponse']
        self.assertEqual(
            set(schema['properties']),
            {'job_id', 'scan_type', 'status'},
        )


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class JobRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    def setUp(self):
        self.schema = 'test_retrieval_' + uuid4().hex
        self.admin = make_engine()
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.drop_schema)

        self.engine = create_engine(
            database_url(),
            connect_args={
                'options': (
                    f'-c search_path={self.schema} -c timezone=UTC'
                ),
            },
        )
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        config = Config('alembic.ini')
        with self.engine.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')

        self.previous = dict(app.dependency_overrides)
        self.addCleanup(self.restore_overrides)
        app.dependency_overrides[get_session_factory] = (
            lambda: self.sessions
        )
        app.dependency_overrides[get_token_validator] = (
            lambda: self.tokens.validator()
        )
        app.dependency_overrides[get_team_settings] = (
            lambda: TeamSettings()
        )
        self.resolver = Mock(return_value=('8.8.8.8',))
        self.policy = TargetPolicy(
            ['https://approved.example.invalid'], self.resolver,
        )
        app.dependency_overrides[get_target_policy] = lambda: self.policy

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA "{self.schema}" CASCADE'
            )

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous)

    def authorization(self, subject='alice', teams=('team-alpha',)):
        return 'Bearer ' + self.tokens.sign(
            self.tokens.claims(sub=subject, team_ids=list(teams))
        )

    def create_job(self, team='team-alpha', creator='alice'):
        return accept_scan(
            self.sessions,
            submission=ZapSubmission.model_validate({
                'scan_type': 'zap',
                'parameters': {
                    'url': 'https://approved.example.invalid/',
                },
            }),
            context=TeamContext(team_id=team, created_by=creator),
        ).job_id

    def retrieve(self, job_id, authorization=None, headers=()):
        return asyncio.run(get(
            app,
            self.authorization() if authorization is None
            else authorization,
            path=f'/scan/{job_id}',
            extra_headers=headers,
        ))

    def counts(self):
        with self.sessions() as session:
            return (
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(
                    select(func.count()).select_from(DispatchIntent)
                ),
            )

    def test_submitted_job_is_readable_by_owner_and_teammate(self):
        status, body = asyncio.run(post_json(
            app,
            {
                'scan_type': 'zap',
                'parameters': {
                    'url': 'https://approved.example.invalid/',
                },
            },
            self.authorization(),
        ))
        self.assertEqual(status, 202)
        accepted = json.loads(body)
        self.resolver.reset_mock()

        for subject in ('alice', 'alice-peer'):
            status, _, body = self.retrieve(
                accepted['job_id'], self.authorization(subject),
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), accepted)

        self.resolver.assert_not_called()
        self.assertEqual(self.counts(), (1, 1))

    def test_two_teams_are_isolated_with_identical_not_found_responses(self):
        alpha = self.create_job()
        beta = self.create_job('team-beta', 'bob')
        unknown = uuid4()

        for own, other, authorization in (
            (alpha, beta, self.authorization()),
            (beta, alpha, self.authorization('bob', ('team-beta',))),
        ):
            status, _, body = self.retrieve(own, authorization)
            self.assertEqual(status, 200)
            self.assertEqual(UUID(json.loads(body)['job_id']), own)

            hidden = self.retrieve(other, authorization)
            missing = self.retrieve(unknown, authorization)
            self.assertEqual(hidden, missing)
            self.assertEqual(hidden[0], 404)
            self.assertEqual(
                json.loads(hidden[2]), {'detail': 'Job not found'},
            )

        self.assertEqual(self.counts(), (2, 2))

    def test_invalid_uuid_and_unauthorized_callers_do_not_query_jobs(self):
        job_id = self.create_job()
        with patch('app.api.routes.scans.find_team_job') as lookup:
            status, _, body = self.retrieve('not-a-uuid')
            self.assertEqual(status, 422)
            errors = json.loads(body)['detail']
            self.assertTrue(any(
                error['loc'] == ['path', 'job_id'] for error in errors
            ))

            cases = (
                (None, (), 401),
                ('Bearer malformed', (), 401),
                (self.authorization(teams=()), (), 403),
                (
                    self.authorization(teams=('team-alpha', 'team-beta')),
                    (),
                    403,
                ),
                (
                    self.authorization(),
                    ((b'x-team-id', b'team-beta'),),
                    403,
                ),
            )
            for authorization, headers, expected in cases:
                status, _, _ = asyncio.run(get(
                    app,
                    authorization,
                    path=f'/scan/{job_id}',
                    extra_headers=headers,
                ))
                self.assertEqual(status, expected)
            lookup.assert_not_called()

        self.assertEqual(self.counts(), (1, 1))

    def test_multi_team_selection_controls_visibility(self):
        job_id = self.create_job()
        authorization = self.authorization(
            teams=('team-alpha', 'team-beta'),
        )
        for selected, expected in (
            (b'team-alpha', 200),
            (b'team-beta', 404),
        ):
            status, _, _ = self.retrieve(
                job_id,
                authorization,
                ((b'x-team-id', selected),),
            )
            self.assertEqual(status, expected)

    def test_reads_current_persisted_status(self):
        job_id = self.create_job()
        status, _, body = self.retrieve(job_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'pending')

        # Test fixture only: no worker or scanner executes.
        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            job.status = 'running'
            job.started_at = datetime.now(timezone.utc)

        status, _, body = self.retrieve(job_id)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['status'], 'running')
        self.assertEqual(
            set(json.loads(body)), {'job_id', 'scan_type', 'status'},
        )

    def test_database_failure_returns_safe_503(self):
        job_id = self.create_job()
        failure = OperationalError(
            'sensitive SQL', {}, Exception('sensitive connection details'),
        )
        with patch(
            'app.api.routes.scans.find_team_job',
            side_effect=failure,
        ):
            status, _, body = self.retrieve(job_id)
        self.assertEqual(status, 503)
        self.assertEqual(
            json.loads(body),
            {'detail': 'Job retrieval is temporarily unavailable'},
        )
        self.assertEqual(self.counts(), (1, 1))
