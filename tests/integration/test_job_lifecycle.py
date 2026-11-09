"""Guarded transitions against real isolated PostgreSQL storage."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.db.session import database_url, make_engine, make_session_factory
from app.models import Job
from app.repositories.lifecycle import (
    fail_pending_job,
    finish_job,
    start_job,
)


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_lifecycle_' + uuid4().hex
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

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(
                f'DROP SCHEMA "{self.schema}" CASCADE'
            )

    def create_job(self):
        with self.sessions.begin() as session:
            job = Job(
                team_id='team-alpha',
                created_by='alice',
                scan_type='zap',
                parameters={'url': 'https://approved.example.invalid/'},
            )
            session.add(job)
            session.flush()
            job_id = job.job_id
        return job_id

    def snapshot(self, job_id):
        with self.sessions() as session:
            job = session.get(Job, job_id)
            return {
                column.name: getattr(job, column.name)
                for column in Job.__table__.columns
            }

    def apply(self, function, **values):
        with self.sessions.begin() as session:
            return function(session, **values)

    def race(self, first, second):
        barrier = Barrier(2)

        def execute(operation):
            with self.sessions.begin() as session:
                barrier.wait(timeout=5)
                return operation(session)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(execute, operation)
                for operation in (first, second)
            ]
            return [future.result(timeout=10) for future in futures]

    def test_running_and_terminal_updates_preserve_ownership_and_utc(self):
        for terminal in ('completed', 'failed'):
            job_id = self.create_job()
            before = self.snapshot(job_id)
            self.assertTrue(self.apply(
                start_job, job_id=job_id, worker_id='worker-a',
            ))
            running = self.snapshot(job_id)
            self.assertEqual(running['status'], 'running')
            self.assertEqual(running['attempt_count'], 1)
            self.assertEqual(running['worker_id'], 'worker-a')
            self.assertIsNotNone(running['started_at'])
            self.assertIsNone(running['finished_at'])

            outcome = (
                {'result': {'high': 1}}
                if terminal == 'completed'
                else {'error': {'code': 'fixture_failure'}}
            )
            self.assertTrue(self.apply(
                finish_job,
                job_id=job_id,
                worker_id='worker-a',
                status=terminal,
                **outcome,
            ))
            finished = self.snapshot(job_id)
            self.assertEqual(finished['status'], terminal)
            for name, value in outcome.items():
                self.assertEqual(finished[name], value)
            unused = 'error' if terminal == 'completed' else 'result'
            self.assertIsNone(finished[unused])
            self.assertGreaterEqual(
                finished['finished_at'], finished['started_at'],
            )
            self.assertGreaterEqual(
                finished['updated_at'], running['updated_at'],
            )
            for name in (
                'created_at', 'updated_at', 'started_at', 'finished_at',
            ):
                self.assertEqual(
                    finished[name].utcoffset().total_seconds(), 0,
                )
            for name in (
                'job_id', 'team_id', 'created_by', 'scan_type',
                'parameters', 'created_at',
            ):
                self.assertEqual(finished[name], before[name])

    def test_competing_starts_have_only_one_winner(self):
        job_id = self.create_job()
        results = self.race(
            lambda session: start_job(
                session, job_id=job_id, worker_id='worker-a',
            ),
            lambda session: start_job(
                session, job_id=job_id, worker_id='worker-b',
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        stored = self.snapshot(job_id)
        self.assertEqual(stored['attempt_count'], 1)
        winner = 'worker-a' if results[0] else 'worker-b'
        loser = 'worker-b' if results[0] else 'worker-a'
        self.assertEqual(stored['worker_id'], winner)
        self.assertFalse(self.apply(
            finish_job,
            job_id=job_id,
            worker_id=loser,
            status='completed',
            result={'high': 0},
        ))
        self.assertEqual(self.snapshot(job_id), stored)

    def test_competing_terminal_writes_preserve_the_winning_outcome(self):
        job_id = self.create_job()
        self.apply(start_job, job_id=job_id, worker_id='worker-a')
        results = self.race(
            lambda session: finish_job(
                session,
                job_id=job_id,
                worker_id='worker-a',
                status='completed',
                result={'high': 1},
            ),
            lambda session: finish_job(
                session,
                job_id=job_id,
                worker_id='worker-a',
                status='failed',
                error={'code': 'fixture_failure'},
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        stored = self.snapshot(job_id)
        self.assertEqual(
            stored['status'], 'completed' if results[0] else 'failed',
        )
        self.assertFalse(self.apply(
            finish_job,
            job_id=job_id,
            worker_id='worker-a',
            status='completed',
            result={'high': 99},
        ))
        self.assertEqual(self.snapshot(job_id), stored)

    def test_all_terminal_states_reject_reopening_and_replacement(self):
        for terminal in ('completed', 'failed', 'cancelled'):
            job_id = self.create_job()
            # Direct fixture setup, not production cancellation behavior.
            with self.sessions.begin() as session:
                job = session.get(Job, job_id)
                job.status = terminal
                job.finished_at = datetime.now(timezone.utc)
                job.result = {} if terminal == 'completed' else None
                job.error = {} if terminal == 'failed' else None
                job.worker_id = 'worker-a'
            stored = self.snapshot(job_id)
            self.assertFalse(self.apply(
                start_job, job_id=job_id, worker_id='worker-a',
            ))
            self.assertFalse(self.apply(
                finish_job,
                job_id=job_id,
                worker_id='worker-a',
                status='failed',
                error={'code': 'late_failure'},
            ))
            self.assertFalse(self.apply(
                fail_pending_job,
                job_id=job_id,
                error={'code': 'late_dispatch_failure'},
            ))
            self.assertEqual(self.snapshot(job_id), stored)

    def test_pending_failure_races_with_start_without_overwriting_running(self):
        failed_id = self.create_job()
        self.assertTrue(self.apply(
            fail_pending_job,
            job_id=failed_id,
            error={'code': 'dispatch_exhausted'},
        ))
        failed = self.snapshot(failed_id)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['error'], {'code': 'dispatch_exhausted'})
        self.assertIsNone(failed['result'])
        self.assertIsNone(failed['started_at'])
        self.assertIsNotNone(failed['finished_at'])
        self.assertEqual(failed['finished_at'].utcoffset().total_seconds(), 0)
        self.assertEqual(failed['attempt_count'], 0)

        job_id = self.create_job()
        results = self.race(
            lambda session: start_job(
                session, job_id=job_id, worker_id='worker-a',
            ),
            lambda session: fail_pending_job(
                session,
                job_id=job_id,
                error={'code': 'dispatch_exhausted'},
            ),
        )
        self.assertEqual(sorted(results), [False, True])
        stored = self.snapshot(job_id)
        self.assertEqual(
            stored['status'], 'running' if results[0] else 'failed',
        )
        if stored['status'] == 'failed':
            self.assertIsNone(stored['started_at'])
            self.assertIsNotNone(stored['finished_at'])
            self.assertEqual(stored['error'], {'code': 'dispatch_exhausted'})
            self.assertEqual(stored['attempt_count'], 0)

    def test_invalid_inputs_and_missing_jobs_do_not_transition(self):
        job_id = self.create_job()
        stored = self.snapshot(job_id)
        for worker_id in ('', ' ', ' padded'):
            with self.assertRaises(ValueError):
                self.apply(
                    start_job, job_id=job_id, worker_id=worker_id,
                )
        for status, outcome in (
            ('completed', {}),
            ('failed', {}),
            ('cancelled', {}),
            ('completed', {'result': {}, 'error': {}}),
        ):
            with self.assertRaises(ValueError):
                self.apply(
                    finish_job,
                    job_id=job_id,
                    worker_id='worker-a',
                    status=status,
                    **outcome,
                )
        with self.assertRaises(ValueError):
            self.apply(fail_pending_job, job_id=job_id, error=None)
        self.assertFalse(self.apply(
            start_job, job_id=uuid4(), worker_id='worker-a',
        ))
        self.assertFalse(self.apply(
            finish_job,
            job_id=job_id,
            worker_id='worker-a',
            status='completed',
            result={},
        ))
        self.assertEqual(self.snapshot(job_id), stored)

        # Reserved cancellation metadata is only a start guard here.
        with self.sessions.begin() as session:
            job = session.get(Job, job_id)
            job.cancellation_requested_at = datetime.now(timezone.utc)
            job.cancellation_requested_by = 'alice'
        blocked = self.snapshot(job_id)
        self.assertFalse(self.apply(
            start_job, job_id=job_id, worker_id='worker-a',
        ))
        self.assertEqual(self.snapshot(job_id), blocked)

    def test_caller_rollback_and_invalid_database_outcome_preserve_state(self):
        job_id = self.create_job()
        pending = self.snapshot(job_id)
        with self.assertRaises(RuntimeError):
            with self.sessions.begin() as session:
                self.assertTrue(start_job(
                    session, job_id=job_id, worker_id='worker-a',
                ))
                raise RuntimeError('Injected caller failure')
        self.assertEqual(self.snapshot(job_id), pending)

        self.apply(start_job, job_id=job_id, worker_id='worker-a')
        running = self.snapshot(job_id)
        with self.assertRaises(IntegrityError):
            with self.sessions.begin() as session:
                job = session.get(Job, job_id)
                job.status = 'completed'
                job.finished_at = datetime.now(timezone.utc)
                # A terminal completion without a result violates storage rules.
                session.flush()
        self.assertEqual(self.snapshot(job_id), running)

        with self.assertRaises(RuntimeError):
            with self.sessions.begin() as session:
                self.assertTrue(finish_job(
                    session,
                    job_id=job_id,
                    worker_id='worker-a',
                    status='completed',
                    result={'high': 1},
                ))
                raise RuntimeError('Injected terminal transaction failure')
        self.assertEqual(self.snapshot(job_id), running)
