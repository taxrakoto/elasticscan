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
from app.core.failure import safe_error
from app.core.target_policy import TargetPolicy, get_target_policy
from app.core.team import TeamContext, TeamSettings, get_team_settings
from app.db.session import database_url, make_engine, make_session_factory
from app.main import app
from app.models import DispatchIntent, Job
from app.repositories.lifecycle import (
    claim_job, complete_job, fail_exhausted_job, fail_job,
)
from app.scanners.base import ScanRequest, ScannerOutput, SeverityCounts
from app.scanners.zap_outcomes import execute_zap_scan, normalize_zap_output
from app.schemas import ZapSubmission
from app.services.acceptance import accept_scan
from tests.auth_support import get
from tests.integration.test_submission_validation import post_json
from tests.token_fixtures import TokenFixtures
from tests.retrieval_contract import (
    PUBLIC_FIELDS, TIMESTAMP_FIELDS, retrieval_example,
)


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
            PUBLIC_FIELDS,
        )
        self.assertEqual(set(schema['required']), PUBLIC_FIELDS)


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
            retrieved = json.loads(body)
            self.assertEqual(set(retrieved), PUBLIC_FIELDS)
            for field, value in accepted.items():
                self.assertEqual(retrieved[field], value)
            self.assertIsNone(retrieved['result'])
            self.assertIsNone(retrieved['error'])
            self.assertIsNone(retrieved['started_at'])
            self.assertIsNone(retrieved['finished_at'])

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
            set(json.loads(body)), PUBLIC_FIELDS,
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

    def claim(self, job_id):
        with self.sessions.begin() as session:
            token = claim_job(
                session, job_id=job_id,
                worker_id='worker-a', lease_seconds=300,
            )
        self.assertIsNotNone(token)
        return token

    def assert_public_job(self, job_id, state, result=None, error=None):
        status, _, raw = self.retrieve(job_id)
        self.assertEqual(status, 200)
        body = json.loads(raw)
        self.assertEqual(set(body), PUBLIC_FIELDS)
        self.assertEqual(body['job_id'], str(job_id))
        self.assertEqual(body['scan_type'], 'zap')
        self.assertEqual(body['status'], state)
        self.assertEqual(body['result'], result)
        self.assertEqual(body['error'], error)
        with self.sessions() as session:
            job = session.get(Job, job_id)
            for field in TIMESTAMP_FIELDS:
                value = body[field]
                stored = getattr(job, field)
                if stored is None:
                    self.assertIsNone(value)
                else:
                    parsed = datetime.fromisoformat(
                        value.replace('Z', '+00:00')
                    )
                    self.assertEqual(parsed, stored)
                    self.assertEqual(parsed.utcoffset().total_seconds(), 0)
            self.assertEqual(
                body['cancellation_requested_by'],
                job.cancellation_requested_by,
            )
        return body

    def test_all_lifecycle_states_are_public_and_team_scoped(self):
        for state in (
            'pending', 'running', 'completed', 'failed', 'cancelled',
        ):
            with self.subTest(state=state):
                job_id = self.create_job()
                expected = retrieval_example(state)
                if state in {'running', 'completed', 'failed'}:
                    token = self.claim(job_id)
                if state == 'completed':
                    with self.sessions.begin() as session:
                        self.assertTrue(complete_job(
                            session, job_id=job_id,
                            worker_id='worker-a', claim_token=token,
                            counts=SeverityCounts(high=1, medium=2, low=3),
                        ))
                elif state == 'failed':
                    adapter = Mock()
                    adapter.run.side_effect = TimeoutError(
                        'Traceback password=secret'
                    )
                    outcome = execute_zap_scan(
                        adapter,
                        ScanRequest(
                            job_id=job_id,
                            url='https://approved.example.invalid/',
                            claim_token=token,
                            scope_regex=r'^https://approved\.example\.invalid/',
                        ),
                    )
                    with self.sessions.begin() as session:
                        self.assertTrue(fail_job(
                            session, job_id=job_id,
                            worker_id='worker-a', claim_token=token,
                            outcome=outcome,
                        ))
                elif state == 'cancelled':
                    # Stored fixture only; no cancellation operation runs.
                    with self.sessions.begin() as session:
                        job = session.get(Job, job_id)
                        for field in (
                            'status', 'finished_at',
                            'cancellation_requested_by',
                            'cancellation_requested_at', 'cancelled_at',
                        ):
                            setattr(job, field, expected[field])

                self.engine.dispose()
                self.assert_public_job(
                    job_id, state,
                    result=expected['result'], error=expected['error'],
                )
                hidden = self.retrieve(
                    job_id, self.authorization('bob', ('team-beta',)),
                )
                missing = self.retrieve(
                    uuid4(), self.authorization('bob', ('team-beta',)),
                )
                self.assertEqual(hidden, missing)
                self.assertEqual(hidden[0], 404)

    def test_normalized_zap_results_are_returned_over_http(self):
        for code in (0, 1, 2):
            with self.subTest(code=code):
                job_id = self.create_job()
                token = self.claim(job_id)
                risks = [] if code == 0 else ['3', '2', '1']
                outcome = normalize_zap_output(ScannerOutput(
                    return_code=code,
                    report=json.dumps({
                        'site': [{
                            'alerts': [{'riskcode': risk} for risk in risks],
                        }],
                    }),
                    passive_complete=True,
                    active_scan_count=0,
                    visited_urls=('https://approved.example.invalid/',),
                ))
                with self.sessions.begin() as session:
                    self.assertTrue(complete_job(
                        session, job_id=job_id,
                        worker_id='worker-a', claim_token=token,
                        counts=outcome.counts,
                    ))
                count = 0 if code == 0 else 1
                self.assert_public_job(
                    job_id, 'completed',
                    result=dict(high=count, medium=count, low=count),
                )

    def test_exhausted_failure_before_start_has_null_start(self):
        for code in ('dispatch_exhausted', 'recovery_exhausted'):
            job_id = self.create_job()
            with self.sessions.begin() as session:
                self.assertTrue(fail_exhausted_job(
                    session, job_id=job_id, code=code,
                ))
            body = self.assert_public_job(
                job_id, 'failed', error=safe_error(code),
            )
            self.assertIsNone(body['started_at'])

    def test_legacy_messages_are_safe_and_invalid_storage_returns_503(self):
        job_id = self.create_job()
        with self.sessions.begin() as session:
            self.assertTrue(fail_exhausted_job(
                session, job_id=job_id, code='dispatch_exhausted',
            ))
        with self.sessions.begin() as session:
            session.get(Job, job_id).error = {
                'code': 'dispatch_exhausted',
                'message': 'Traceback password=secret',
                'report': 'Bearer secret-token',
            }
        self.assert_public_job(
            job_id, 'failed', error=safe_error('dispatch_exhausted'),
        )
        with self.sessions.begin() as session:
            session.get(Job, job_id).error = {
                'code': 'unknown', 'message': 'password=secret',
            }
        status, _, raw = self.retrieve(job_id)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {
            'detail': 'Job retrieval is temporarily unavailable',
        })

        completed_id = self.create_job()
        token = self.claim(completed_id)
        with self.sessions.begin() as session:
            self.assertTrue(complete_job(
                session, job_id=completed_id,
                worker_id='worker-a', claim_token=token,
                counts=SeverityCounts(),
            ))
        with self.sessions.begin() as session:
            session.get(Job, completed_id).result = {
                'high': -1, 'medium': 0, 'low': 0,
            }
        status, _, raw = self.retrieve(completed_id)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {
            'detail': 'Job retrieval is temporarily unavailable',
        })

    @unittest.skipUnless(
        os.environ.get('REAL_ZAP_RESULT_JSON'),
        'Use scripts/verify_result_summaries.py for actual ZAP counts',
    )
    def test_actual_zap_counts_are_returned_over_http(self):
        summary = json.loads(os.environ['REAL_ZAP_RESULT_JSON'])
        job_id = self.create_job()
        token = self.claim(job_id)
        with self.sessions.begin() as session:
            self.assertTrue(complete_job(
                session, job_id=job_id,
                worker_id='worker-a', claim_token=token,
                counts=SeverityCounts(**summary),
            ))
        self.engine.dispose()
        self.assert_public_job(job_id, 'completed', result=summary)
