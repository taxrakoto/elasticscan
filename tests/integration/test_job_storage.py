"""Real PostgreSQL checks; each test migrates its own disposable schema."""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.session import database_url, make_engine, make_session_factory
from app.models import DispatchIntent, Job
from app.schemas import ZapSubmission


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
