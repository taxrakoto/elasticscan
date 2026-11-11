"""Atomic, validated completion against disposable PostgreSQL schemas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
from threading import Barrier
import unittest
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.team import TeamContext
from app.db.session import database_url, make_engine, make_session_factory
from app.models import Job
from app.repositories.jobs import find_team_job
from app.repositories.lifecycle import claim_job, complete_job
from app.scanners.base import ScannerOutput, SeverityCounts
from app.scanners.zap_outcomes import normalize_zap_output


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class ResultSummaryTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_result_summaries_' + uuid4().hex
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

    def new_engine(self):
        return create_engine(
            database_url(),
            connect_args={
                'options': f'-c search_path={self.schema} -c timezone=UTC',
            },
        )

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')

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
            return job.job_id

    def claim(self, job_id):
        with self.sessions.begin() as session:
            return claim_job(
                session,
                job_id=job_id,
                worker_id='worker-a',
                lease_seconds=300,
            )

    def snapshot(self, job_id):
        with self.sessions() as session:
            job = session.get(Job, job_id)
            return {
                column.name: getattr(job, column.name)
                for column in Job.__table__.columns
            }

    def complete(self, job_id, token, counts, worker='worker-a'):
        with self.sessions.begin() as session:
            return complete_job(
                session,
                job_id=job_id,
                worker_id=worker,
                claim_token=token,
                counts=counts,
            )

    def test_normalized_findings_and_zero_counts_survive_new_connections(self):
        for code in (0, 1, 2):
            with self.subTest(code=code):
                job_id = self.create_job()
                before = self.snapshot(job_id)
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
                self.assertEqual(outcome.status, 'completed')
                self.assertTrue(self.complete(job_id, token, outcome.counts))
                expected = (
                    {'high': 0, 'medium': 0, 'low': 0}
                    if code == 0 else
                    {'high': 1, 'medium': 1, 'low': 1}
                )
                self.assert_retrieved_after_replacement(job_id, expected)
                stored = self.snapshot(job_id)
                for field in (
                    'job_id', 'team_id', 'created_by', 'scan_type',
                    'parameters', 'created_at',
                ):
                    self.assertEqual(stored[field], before[field])
                self.assertIsNone(stored['claim_token'])
                self.assertIsNone(stored['lease_expires_at'])
                self.assertIsNone(stored['error'])
                self.assertGreaterEqual(
                    stored['finished_at'], stored['started_at'],
                )
                self.assertEqual(
                    stored['finished_at'].utcoffset(), timedelta(0),
                )

    def assert_retrieved_after_replacement(self, job_id, expected):
        self.engine.dispose()
        replacement = self.new_engine()
        try:
            sessions = make_session_factory(replacement)
            with sessions() as session:
                job = find_team_job(
                    session,
                    job_id=job_id,
                    context=TeamContext('team-alpha', 'alice-peer'),
                )
                self.assertEqual(job.status, 'completed')
                self.assertEqual(job.result, expected)
                self.assertIsNone(job.error)
                self.assertIsNotNone(job.finished_at)
                self.assertIsNone(find_team_job(
                    session,
                    job_id=job_id,
                    context=TeamContext('team-beta', 'bob'),
                ))
        finally:
            replacement.dispose()

    def test_invalid_counts_and_token_leave_job_unchanged(self):
        job_id = self.create_job()
        token = self.claim(job_id)
        before = self.snapshot(job_id)
        for value in (-1, True, 1.5, '1', None):
            for field in ('high', 'medium', 'low'):
                with self.subTest(value=value, field=field):
                    with self.assertRaises(ValueError):
                        self.complete(
                            job_id, token, SeverityCounts(**{field: value}),
                        )
        for counts in (None, {'high': 1, 'medium': 0, 'low': 0}):
            with self.assertRaises(ValueError):
                self.complete(job_id, token, counts)
        for invalid_token in (None, 'invalid'):
            with self.assertRaises(ValueError):
                self.complete(job_id, invalid_token, SeverityCounts())
        self.assertEqual(self.snapshot(job_id), before)

    def test_uncommitted_completion_and_database_failure_are_atomic(self):
        job_id = self.create_job()
        token = self.claim(job_id)
        before = self.snapshot(job_id)
        with self.assertRaises(SQLAlchemyError):
            with self.sessions.begin() as session:
                self.assertTrue(complete_job(
                    session,
                    job_id=job_id,
                    worker_id='worker-a',
                    claim_token=token,
                    counts=SeverityCounts(high=2),
                ))
                # Another connection still sees the whole running state.
                self.assertEqual(self.snapshot(job_id), before)
                session.execute(text('SELECT 1 / 0'))
        self.assertEqual(self.snapshot(job_id), before)

    def test_state_owner_token_expiry_and_duplicate_guards(self):
        job_id = self.create_job()
        pending = self.snapshot(job_id)
        self.assertFalse(self.complete(job_id, uuid4(), SeverityCounts()))
        self.assertEqual(self.snapshot(job_id), pending)
        token = self.claim(job_id)
        running = self.snapshot(job_id)
        self.assertFalse(self.complete(
            job_id, token, SeverityCounts(), worker='worker-b',
        ))
        self.assertFalse(self.complete(job_id, uuid4(), SeverityCounts()))
        self.assertFalse(self.complete(uuid4(), token, SeverityCounts()))
        self.assertEqual(self.snapshot(job_id), running)
        self.assertTrue(self.complete(job_id, token, SeverityCounts(high=1)))
        completed = self.snapshot(job_id)
        self.assertFalse(self.complete(job_id, token, SeverityCounts(high=99)))
        self.assertEqual(self.snapshot(job_id), completed)

        expired_id = self.create_job()
        expired_token = self.claim(expired_id)
        with self.sessions.begin() as session:
            session.get(Job, expired_id).lease_expires_at = (
                session.scalar(select(func.clock_timestamp()))
                - timedelta(seconds=1)
            )
        expired = self.snapshot(expired_id)
        self.assertFalse(self.complete(
            expired_id, expired_token, SeverityCounts(),
        ))
        self.assertEqual(self.snapshot(expired_id), expired)

    def test_competing_completions_preserve_one_whole_summary(self):
        job_id = self.create_job()
        token = self.claim(job_id)
        barrier = Barrier(2)
        summaries = (
            SeverityCounts(high=1, medium=2, low=3),
            SeverityCounts(high=4, medium=5, low=6),
        )

        def finish(counts):
            barrier.wait(timeout=5)
            return self.complete(job_id, token, counts)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finish, summaries))
        self.assertEqual(sorted(results), [False, True])
        winner = summaries[results.index(True)]
        self.assertEqual(self.snapshot(job_id)['result'], {
            'high': winner.high,
            'medium': winner.medium,
            'low': winner.low,
        })

    @unittest.skipUnless(
        os.environ.get('REAL_ZAP_RESULT_JSON'),
        'Use scripts/verify_result_summaries.py for actual adapter output',
    )
    def test_actual_adapter_summary_is_persisted_and_retrievable(self):
        summary = json.loads(os.environ['REAL_ZAP_RESULT_JSON'])
        job_id = self.create_job()
        token = self.claim(job_id)
        self.assertTrue(self.complete(
            job_id, token, SeverityCounts(**summary),
        ))
        self.assert_retrieved_after_replacement(job_id, summary)
