"""Real PostgreSQL checks; each test migrates its own disposable schema."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, func, inspect, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core.team import TeamContext, TeamSettings, resolve_team_context
from app.db.base import Base
from app.db.session import database_url, make_engine, make_session_factory
from app.models import DispatchIntent, Job
from app.repositories.jobs import create_pending_job
from app.schemas import ZapSubmission
from app.services.acceptance import accept_scan
from tests.token_fixtures import TokenFixtures


@unittest.skipUnless(os.environ.get('RUN_POSTGRES_TESTS') == '1', 'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests')
class JobStorageTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_storage_' + uuid4().hex
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
        self.migrate('upgrade', 'head')

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')

    def migrate(self, direction, revision):
        config = Config('alembic.ini')
        with self.engine.begin() as connection:
            config.attributes['connection'] = connection
            getattr(command, direction)(config, revision)

    def new_job(self, **changes):
        submission = ZapSubmission.model_validate({
            'scan_type': 'zap', 'parameters': {'url': 'https://example.com/'},
        })
        values = dict(team_id='team-alpha', created_by='fixture-subject',
                      scan_type=submission.scan_type,
                      parameters=submission.parameters.model_dump(mode='json'))
        values.update(changes)
        return Job(**values)

    def save_job(self):
        with self.sessions.begin() as session:
            job = self.new_job()
            session.add(job)
            session.flush()
            session.add(DispatchIntent(job_id=job.job_id))
        return job.job_id

    def test_migrated_database_persists_and_reloads_job_and_dispatch_intent(self):
        job_id = self.save_job()
        # Reopen through a fresh pool, not the creating session's cache.
        self.engine.dispose()
        with self.sessions() as session:
            job = session.get(Job, job_id)
            intent = session.scalar(select(DispatchIntent).where(DispatchIntent.job_id == job_id))
            self.assertEqual(job.team_id, 'team-alpha')
            self.assertEqual(job.created_by, 'fixture-subject')
            self.assertEqual(job.scan_type, 'zap')
            self.assertEqual(job.parameters, {'url': 'https://example.com/'})
            self.assertEqual(job.status, 'pending')
            self.assertEqual(job.attempt_count, 0)
            self.assertIsNone(job.result)
            self.assertIsNone(job.error)
            self.assertIsNone(job.claim_token)
            self.assertIsNone(job.cancellation_requested_at)
            for timestamp in (job.created_at, job.updated_at, intent.created_at, intent.next_attempt_at):
                self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
            self.assertEqual(intent.schema_version, 1)
            self.assertEqual(intent.attempt_count, 0)
            self.assertIsNone(intent.published_at)

    def save_owned_job(
        self,
        subject='alice',
        teams=('team-alpha',),
        selectors=None,
    ):
        tokens = TokenFixtures()
        token = tokens.sign(
            tokens.claims(sub=subject, team_ids=list(teams)),
        )
        verified_claims = tokens.validator().validate(token)
        context = resolve_team_context(
            verified_claims, TeamSettings(), selectors,
        )
        submission = ZapSubmission.model_validate({
            'scan_type': 'zap',
            'parameters': {'url': 'https://example.com/'},
        })

        with self.sessions.begin() as session:
            job = create_pending_job(
                session, submission=submission, context=context,
            )
            job_id = job.job_id

        return job_id

    def test_creation_persists_verified_team_and_creator(self):
        cases = (
            ('alice', ('team-alpha',), None, 'team-alpha'),
            ('alice-peer', ('team-alpha',), None, 'team-alpha'),
            ('bob', ('team-beta',), None, 'team-beta'),
            (
                'multi-team-user',
                ('team-alpha', 'team-beta'),
                ['team-beta'],
                'team-beta',
            ),
        )

        for subject, teams, selectors, expected_team in cases:
            with self.subTest(subject=subject):
                job_id = self.save_owned_job(
                    subject=subject, teams=teams, selectors=selectors,
                )

                # A fresh connection pool rules out session-cache evidence.
                self.engine.dispose()
                with self.sessions() as session:
                    job = session.get(Job, job_id)
                    self.assertIsNotNone(job)
                    self.assertEqual(job.team_id, expected_team)
                    self.assertEqual(job.created_by, subject)
                    self.assertEqual(job.status, 'pending')
                    self.assertEqual(job.scan_type, 'zap')
                    self.assertEqual(
                        job.parameters, {'url': 'https://example.com/'},
                    )

    def test_ownership_transfer_is_rejected_for_all_update_paths(self):
        job_id = self.save_owned_job()
        changes = (
            ('team_id', 'team-beta'),
            ('created_by', 'other-subject'),
        )

        for mode in ('orm', 'bulk', 'sql'):
            for field, value in changes:
                with self.subTest(mode=mode, field=field):
                    with self.assertRaises(IntegrityError) as error:
                        with self.sessions.begin() as session:
                            if mode == 'orm':
                                job = session.get(Job, job_id)
                                setattr(job, field, value)
                                session.flush()
                            elif mode == 'bulk':
                                session.execute(
                                    update(Job)
                                    .where(Job.job_id == job_id)
                                    .values(**{field: value})
                                )
                            else:
                                # field comes only from the fixed cases above.
                                session.execute(
                                    text(
                                        f'UPDATE jobs SET {field} = :value '
                                        'WHERE job_id = :id'
                                    ),
                                    {'value': value, 'id': job_id},
                                )

                    self.assertEqual(error.exception.orig.sqlstate, '23514')
                    with self.sessions() as session:
                        job = session.get(Job, job_id)
                        self.assertEqual(job.team_id, 'team-alpha')
                        self.assertEqual(job.created_by, 'alice')

    def test_execution_metadata_updates_preserve_ownership(self):
        job_id = self.save_owned_job()

        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            job.worker_id = 'worker-fixture'
            job.attempt_count = 1

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    'UPDATE jobs '
                    'SET heartbeat_at = statement_timestamp() '
                    'WHERE job_id = :id'
                ),
                {'id': job_id},
            )

        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.team_id, 'team-alpha')
            self.assertEqual(job.created_by, 'alice')
            self.assertEqual(job.worker_id, 'worker-fixture')
            self.assertEqual(job.attempt_count, 1)
            self.assertIsNotNone(job.heartbeat_at)

    def accept_fixture(self):
        submission = ZapSubmission.model_validate({
            'scan_type': 'zap',
            'parameters': {'url': 'https://example.com/'},
        })
        return accept_scan(
            self.sessions,
            submission=submission,
            context=TeamContext('team-alpha', 'fixture-subject'),
        )

    def assert_acceptance_counts(self, jobs, intents):
        with self.sessions() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(Job)),
                jobs,
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(DispatchIntent)),
                intents,
            )

    def test_acceptance_commits_job_and_dispatch_intent(self):
        accepted = self.accept_fixture()
        self.assertEqual(accepted.scan_type, 'zap')
        self.assertEqual(accepted.status, 'pending')

        self.engine.dispose()
        with self.sessions() as session:
            job = session.get(Job, accepted.job_id)
            intent = session.scalar(
                select(DispatchIntent).where(
                    DispatchIntent.job_id == accepted.job_id,
                )
            )

            self.assertIsNotNone(job)
            self.assertIsNotNone(intent)
            self.assertEqual(job.team_id, 'team-alpha')
            self.assertEqual(job.created_by, 'fixture-subject')
            self.assertEqual(job.status, 'pending')
            self.assertEqual(job.parameters, {'url': 'https://example.com/'})
            self.assertEqual(intent.schema_version, 1)
            self.assertEqual(intent.attempt_count, 0)
            self.assertIsNone(intent.published_at)
            self.assertIsNone(intent.suppressed_at)

        self.assert_acceptance_counts(1, 1)

    def test_acceptance_rolls_back_failure_after_job_insert(self):
        inserted_ids = []

        def insert_then_fail(session, **kwargs):
            job = create_pending_job(session, **kwargs)
            inserted_ids.append(job.job_id)
            raise RuntimeError('failure after job insert')

        with patch(
            'app.services.acceptance.create_pending_job',
            side_effect=insert_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, 'failure after job insert'):
                self.accept_fixture()

        self.assertEqual(len(inserted_ids), 1)
        self.assert_acceptance_counts(0, 0)

    def test_acceptance_rolls_back_invalid_dispatch_intent(self):
        def invalid_intent(**values):
            return DispatchIntent(schema_version=0, **values)

        with patch(
            'app.services.acceptance.DispatchIntent',
            side_effect=invalid_intent,
        ):
            with self.assertRaises(IntegrityError) as error:
                self.accept_fixture()

        self.assertEqual(error.exception.orig.sqlstate, '23514')
        self.assert_acceptance_counts(0, 0)

    def test_acceptance_rolls_back_failure_before_commit(self):
        observed = []

        def fail_before_commit(session):
            counts = (
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(select(func.count()).select_from(DispatchIntent)),
            )
            observed.append(counts)
            raise RuntimeError('failure before commit')

        event.listen(self.sessions, 'before_commit', fail_before_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, 'failure before commit'):
                self.accept_fixture()
        finally:
            event.remove(self.sessions, 'before_commit', fail_before_commit)

        self.assertEqual(observed, [(1, 1)])
        self.assert_acceptance_counts(0, 0)

    def test_acceptance_records_become_visible_only_after_commit(self):
        observed = []

        def inspect_before_commit(session):
            writer_counts = (
                session.scalar(select(func.count()).select_from(Job)),
                session.scalar(select(func.count()).select_from(DispatchIntent)),
            )
            self.assert_acceptance_counts(0, 0)
            observed.append(writer_counts)

        event.listen(self.sessions, 'before_commit', inspect_before_commit)
        try:
            self.accept_fixture()
        finally:
            event.remove(self.sessions, 'before_commit', inspect_before_commit)

        self.assertEqual(observed, [(1, 1)])
        self.assert_acceptance_counts(1, 1)

    def test_applied_migration_matches_models_and_can_be_reapplied(self):
        self.migrate('upgrade', 'head')
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), Base.metadata), [])
            self.assertEqual(connection.scalar(text('SELECT version_num FROM alembic_version')), '0001')
        self.migrate('downgrade', 'base')
        self.assertNotIn('jobs', inspect(self.engine).get_table_names())
        self.assertNotIn('dispatch_intents', inspect(self.engine).get_table_names())
        self.migrate('upgrade', 'head')
        self.save_job()

    def test_database_rejects_immutable_field_changes_even_through_raw_sql(self):
        job_id = self.save_job()
        changes = {
            'job_id': "'" + str(uuid4()) + "'::uuid",
            'team_id': "'team-beta'", 'created_by': "'other-subject'",
            'scan_type': "'other-tool'", 'parameters': "'{\"url\":\"https://other.example/\"}'::jsonb",
            'created_at': "created_at + interval '1 second'",
        }
        for column, expression in changes.items():
            with self.subTest(column=column), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(text(f'UPDATE jobs SET {column} = {expression} WHERE job_id = :id'), {'id': job_id})
        with self.sessions() as session:
            self.assertEqual(session.get(Job, job_id).team_id, 'team-alpha')

    def test_database_rejects_invalid_job_state_and_missing_ownership(self):
        for changes in (
            {'team_id': None}, {'created_by': None}, {'team_id': '  '},
            {'status': 'unknown'}, {'parameters': []}, {'attempt_count': -1},
            {'status': 'completed'}, {'result': {'high': 0}},
            {'claim_token': uuid4()}, {'status': 'running'},
        ):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                with self.sessions.begin() as session:
                    session.add(self.new_job(**changes))

    def test_execution_and_terminal_outcome_can_be_stored(self):
        job_id = self.save_job()
        now = datetime.now(timezone.utc)
        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            previous_update = job.updated_at
            job.status, job.started_at = 'running', now
            job.worker_id, job.attempt_count = 'worker-fixture', 1
            job.claim_token, job.lease_expires_at = uuid4(), now
            job.heartbeat_at = now
            session.flush()
            self.assertGreater(job.updated_at, previous_update)
        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            job.status, job.finished_at = 'completed', now
            job.result = {'high': 0, 'medium': 1, 'low': 2}
            job.claim_token = job.lease_expires_at = None
        with self.sessions() as session:
            job = session.get(Job, job_id)
            self.assertEqual(job.status, 'completed')
            self.assertEqual(job.result['medium'], 1)
            self.assertEqual(job.attempt_count, 1)
            self.assertGreater(job.updated_at, previous_update)

    def test_dispatch_intent_requires_existing_job_and_is_unique(self):
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                session.add(DispatchIntent(job_id=uuid4()))
        job_id = self.save_job()
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                session.add(DispatchIntent(job_id=job_id))
