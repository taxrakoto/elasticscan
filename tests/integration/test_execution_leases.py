"""Execution claim ownership and expiry against isolated PostgreSQL."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select

from app.db.session import database_url, make_engine, make_session_factory
from app.models import Job
from app.repositories.lifecycle import (
    claim_job,
    finish_job,
    renew_job,
    start_job,
)


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class ExecutionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_execution_leases_' + uuid4().hex
        self.admin = make_engine()
        self.addCleanup(self.admin.dispose)
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.drop_schema)
        self.engine = create_engine(
            database_url(),
            connect_args={
                'options': f'-c search_path={self.schema} -c timezone=UTC',
            },
        )
        self.addCleanup(self.engine.dispose)
        self.sessions = make_session_factory(self.engine)
        config = Config('alembic.ini')
        with self.engine.begin() as connection:
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        with self.sessions.begin() as session:
            job = Job(
                team_id='team-alpha',
                created_by='alice',
                scan_type='zap',
                parameters={'url': 'https://approved.example.invalid/'},
            )
            session.add(job)
            session.flush()
            self.job_id = job.job_id

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')

    def apply(self, function, **values):
        with self.sessions.begin() as session:
            return function(
                session,
                job_id=self.job_id,
                worker_id='worker-a',
                **values,
            )

    def snapshot(self):
        with self.sessions() as session:
            job = session.get(Job, self.job_id)
            return {
                column.name: getattr(job, column.name)
                for column in Job.__table__.columns
            }

    def expire(self):
        with self.sessions.begin() as session:
            job = session.get(Job, self.job_id)
            job.lease_expires_at = session.scalar(
                select(func.clock_timestamp())
            ) - timedelta(seconds=1)

    def test_concurrent_claims_have_one_token_and_preserve_identity(self):
        before = self.snapshot()
        barrier = Barrier(2)

        def claim(worker):
            with self.sessions.begin() as session:
                barrier.wait(timeout=5)
                return claim_job(
                    session,
                    job_id=self.job_id,
                    worker_id=worker,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, ('worker-a', 'worker-b')))
        tokens = [token for token in results if token is not None]
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], UUID)
        stored = self.snapshot()
        self.assertEqual(stored['claim_token'], tokens[0])
        self.assertEqual(stored['attempt_count'], 1)
        self.assertEqual(stored['status'], 'running')
        self.assertEqual(
            stored['lease_expires_at'] - stored['heartbeat_at'],
            timedelta(seconds=60),
        )
        for field in (
            'job_id', 'team_id', 'created_by', 'scan_type', 'parameters',
        ):
            self.assertEqual(stored[field], before[field])
        for field in ('started_at', 'heartbeat_at', 'lease_expires_at'):
            self.assertEqual(stored[field].utcoffset(), timedelta(0))
        self.assertIsNone(self.apply(claim_job))
        self.assertFalse(self.apply(start_job))
        self.assertEqual(self.snapshot(), stored)

    def test_renewal_requires_current_worker_and_token(self):
        token = self.apply(claim_job)
        before = self.snapshot()
        with self.sessions.begin() as session:
            self.assertFalse(renew_job(
                session,
                job_id=self.job_id,
                worker_id='worker-b',
                claim_token=token,
            ))
            self.assertFalse(finish_job(
                session,
                job_id=self.job_id,
                worker_id='worker-b',
                claim_token=token,
                status='completed',
                result={},
            ))
        self.assertFalse(self.apply(renew_job, claim_token=uuid4()))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.apply(
            renew_job, claim_token=token, lease_seconds=120,
        ))
        renewed = self.snapshot()
        self.assertGreater(
            renewed['lease_expires_at'], before['lease_expires_at'],
        )
        self.assertEqual(renewed['attempt_count'], 1)
        self.assertGreaterEqual(
            renewed['heartbeat_at'], before['heartbeat_at'],
        )
        self.assertTrue(self.apply(
            renew_job, claim_token=token, lease_seconds=1,
        ))
        self.assertEqual(
            self.snapshot()['lease_expires_at'], renewed['lease_expires_at'],
        )

    def test_expired_claim_cannot_renew_finish_or_reclaim(self):
        token = self.apply(claim_job)
        self.expire()
        before = self.snapshot()
        self.assertFalse(self.apply(renew_job, claim_token=token))
        self.assertFalse(self.apply(
            finish_job,
            claim_token=token,
            status='completed',
            result={},
        ))
        self.assertIsNone(self.apply(claim_job))
        self.assertEqual(self.snapshot(), before)

    def test_missing_token_and_replaced_token_cannot_finish(self):
        old = self.apply(claim_job)
        before = self.snapshot()
        self.assertFalse(self.apply(
            finish_job, status='completed', result={},
        ))
        self.assertEqual(self.snapshot(), before)

        # Fixture for a future replacement attempt, not recovery behavior.
        replacement = uuid4()
        with self.sessions.begin() as session:
            session.get(Job, self.job_id).claim_token = replacement
        before = self.snapshot()
        self.assertFalse(self.apply(renew_job, claim_token=old))
        self.assertFalse(self.apply(
            finish_job, claim_token=old, status='completed', result={},
        ))
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.apply(
            finish_job,
            claim_token=replacement,
            status='completed',
            result={'high': 0},
        ))

    def test_terminal_outcome_wins_and_clears_active_lease(self):
        token = self.apply(claim_job)
        barrier = Barrier(2)

        def finish(status):
            with self.sessions.begin() as session:
                barrier.wait(timeout=5)
                outcome = (
                    {'result': {'high': 0}}
                    if status == 'completed'
                    else {'error': {'code': 'fixture_failure'}}
                )
                return finish_job(
                    session,
                    job_id=self.job_id,
                    worker_id='worker-a',
                    claim_token=token,
                    status=status,
                    **outcome,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finish, ('completed', 'failed')))
        self.assertEqual(sorted(results), [False, True])
        stored = self.snapshot()
        self.assertIsNone(stored['claim_token'])
        self.assertIsNone(stored['lease_expires_at'])
        self.assertIsNotNone(stored['heartbeat_at'])
        self.assertIsNotNone(stored['finished_at'])
        self.assertFalse(self.apply(renew_job, claim_token=token))
        self.assertFalse(self.apply(
            finish_job, claim_token=token, status='completed', result={},
        ))
        self.assertIsNone(self.apply(claim_job))
        self.assertEqual(self.snapshot(), stored)

    def test_claim_renewal_and_finish_roll_back_with_caller(self):
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            with self.sessions.begin() as session:
                self.assertIsNotNone(claim_job(
                    session, job_id=self.job_id, worker_id='worker-a',
                ))
                raise RuntimeError('Rollback claim')
        self.assertEqual(self.snapshot(), before)

        token = self.apply(claim_job)
        before = self.snapshot()
        with self.assertRaises(RuntimeError):
            with self.sessions.begin() as session:
                self.assertTrue(renew_job(
                    session,
                    job_id=self.job_id,
                    worker_id='worker-a',
                    claim_token=token,
                    lease_seconds=120,
                ))
                raise RuntimeError('Rollback renewal')
        self.assertEqual(self.snapshot(), before)

        with self.assertRaises(RuntimeError):
            with self.sessions.begin() as session:
                self.assertTrue(finish_job(
                    session,
                    job_id=self.job_id,
                    worker_id='worker-a',
                    claim_token=token,
                    status='completed',
                    result={},
                ))
                raise RuntimeError('Rollback finish')
        self.assertEqual(self.snapshot(), before)

    def test_expiry_uses_current_clock_not_transaction_start(self):
        token = self.apply(claim_job)
        with self.sessions.begin() as session:
            transaction_start = session.scalar(select(func.now()))
            with self.sessions.begin() as other:
                later = other.scalar(select(func.clock_timestamp()))
                expiry = transaction_start + (later - transaction_start) / 2
                self.assertGreater(expiry, transaction_start)
                other.get(Job, self.job_id).lease_expires_at = expiry
            self.assertFalse(renew_job(
                session,
                job_id=self.job_id,
                worker_id='worker-a',
                claim_token=token,
            ))
            self.assertFalse(finish_job(
                session,
                job_id=self.job_id,
                worker_id='worker-a',
                claim_token=token,
                status='completed',
                result={},
            ))

    def test_invalid_inputs_missing_jobs_and_cancellation_do_not_claim(self):
        before = self.snapshot()
        for seconds in (0, -1, True, 1.5, '60', 10 ** 30):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    self.apply(claim_job, lease_seconds=seconds)
                with self.assertRaises(ValueError):
                    self.apply(
                        renew_job, claim_token=uuid4(), lease_seconds=seconds,
                    )
        for worker in ('', ' ', ' padded'):
            with self.assertRaises(ValueError):
                with self.sessions.begin() as session:
                    claim_job(
                        session, job_id=self.job_id, worker_id=worker,
                    )
        for token in (None, 'invalid'):
            with self.assertRaises(ValueError):
                self.apply(renew_job, claim_token=token)
        with self.assertRaises(ValueError):
            self.apply(
                finish_job, claim_token='invalid', status='completed', result={},
            )
        with self.sessions.begin() as session:
            self.assertIsNone(claim_job(
                session, job_id=uuid4(), worker_id='worker-a',
            ))
            self.assertFalse(renew_job(
                session,
                job_id=uuid4(),
                worker_id='worker-a',
                claim_token=uuid4(),
            ))
        self.assertEqual(self.snapshot(), before)
        with self.sessions.begin() as session:
            job = session.get(Job, self.job_id)
            job.cancellation_requested_at = session.scalar(
                select(func.clock_timestamp())
            )
            job.cancellation_requested_by = 'alice'
        blocked = self.snapshot()
        self.assertIsNone(self.apply(claim_job))
        self.assertEqual(self.snapshot(), blocked)
