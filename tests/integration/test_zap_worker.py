"""Worker handoff against disposable, migrated PostgreSQL storage."""
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event

from app.db.session import database_url, make_engine, make_session_factory
from app.models import Job
from app.workers.zap import handle_message


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / 'fixtures/contracts/v1/job-message.json'
)


@unittest.skipUnless(
    os.environ.get('RUN_POSTGRES_TESTS') == '1',
    'Set RUN_POSTGRES_TESTS=1 for real PostgreSQL tests',
)
class ZapWorkerTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'test_zap_worker_' + uuid4().hex
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
        self.payload = json.loads(FIXTURE.read_text())
        self.job_id = UUID(self.payload['job_id'])
        self.adapter = Mock()

    def drop_schema(self):
        with self.admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.schema}" CASCADE')

    def save_job(self, **changes):
        values = {
            'job_id': self.job_id,
            'team_id': 'team-alpha',
            'created_by': 'alice',
            'scan_type': 'zap',
            'parameters': self.payload['parameters'].copy(),
        }
        values.update(changes)
        with self.sessions.begin() as session:
            session.add(Job(**values))

    def snapshot(self):
        with self.sessions() as session:
            job = session.get(Job, self.job_id)
            return {
                column.name: getattr(job, column.name)
                for column in Job.__table__.columns
            }

    def handle(self, worker_id='worker-a'):
        return handle_message(
            json.dumps(self.payload),
            sessions=self.sessions,
            worker_id=worker_id,
            adapter=self.adapter,
        )

    def test_adapter_sees_committed_start_and_preserved_ownership(self):
        self.save_job()
        before = self.snapshot()

        def inspect_request(request):
            stored = self.snapshot()
            self.assertEqual(stored['status'], 'running')
            self.assertEqual(stored['worker_id'], 'worker-a')
            self.assertEqual(stored['attempt_count'], 1)
            self.assertIsNotNone(stored['started_at'])
            self.assertEqual(request.job_id, self.job_id)
            self.assertEqual(request.url, self.payload['parameters']['url'])
            for field in (
                'team_id', 'created_by', 'scan_type', 'parameters',
            ):
                self.assertEqual(stored[field], before[field])

        self.adapter.run.side_effect = inspect_request
        self.assertEqual(self.handle(), 'invoked')
        self.adapter.run.assert_called_once()

    def test_missing_and_inconsistent_jobs_never_invoke_adapter(self):
        self.assertEqual(self.handle(), 'missing_job')
        cases = (
            {'scan_type': 'other'},
            {'parameters': {'url': 'https://different.example.invalid/'}},
            {'parameters': {
                **self.payload['parameters'], 'unexpected': True,
            }},
            {'parameters': {'url': 'file:///etc/passwd'}},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.save_job(**changes)
                before = self.snapshot()
                self.assertEqual(self.handle(), 'inconsistent_message')
                self.assertEqual(self.snapshot(), before)
                with self.sessions.begin() as session:
                    session.delete(session.get(Job, self.job_id))
        self.adapter.run.assert_not_called()

    def test_nonstartable_jobs_are_preserved(self):
        for status in ('running', 'completed', 'failed', 'cancelled'):
            with self.subTest(status=status):
                terminal = status != 'running'
                self.save_job(
                    status=status,
                    started_at=datetime.now(timezone.utc),
                    finished_at=datetime.now(timezone.utc) if terminal else None,
                    result={} if status == 'completed' else None,
                    error={} if status == 'failed' else None,
                )
                before = self.snapshot()
                self.assertEqual(self.handle(), 'not_started')
                self.assertEqual(self.snapshot(), before)
                with self.sessions.begin() as session:
                    session.delete(session.get(Job, self.job_id))

        self.save_job(
            cancellation_requested_at=datetime.now(timezone.utc),
            cancellation_requested_by='alice',
        )
        before = self.snapshot()
        self.assertEqual(self.handle(), 'not_started')
        self.assertEqual(self.snapshot(), before)
        self.adapter.run.assert_not_called()

    def test_competing_deliveries_and_redelivery_invoke_once(self):
        self.save_job()
        barrier = Barrier(2)

        def deliver(worker_id):
            barrier.wait(timeout=5)
            return self.handle(worker_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(deliver, ('worker-a', 'worker-b')))
        self.assertEqual(sorted(results), ['invoked', 'not_started'])
        before = self.snapshot()
        self.assertEqual(self.handle(), 'not_started')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(before['attempt_count'], 1)
        self.adapter.run.assert_called_once()

    def test_precommit_failure_prevents_adapter_invocation(self):
        self.save_job()
        before = self.snapshot()

        def fail_commit(session):
            raise RuntimeError('Injected precommit failure')

        event.listen(self.sessions, 'before_commit', fail_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, 'precommit'):
                self.handle()
        finally:
            event.remove(self.sessions, 'before_commit', fail_commit)

        self.adapter.run.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_adapter_exception_propagates_after_durable_start(self):
        self.save_job()
        self.adapter.run.side_effect = RuntimeError('Injected adapter failure')
        with self.assertRaisesRegex(RuntimeError, 'adapter failure'):
            self.handle()
        stored = self.snapshot()
        self.assertEqual(stored['status'], 'running')
        self.assertIsNone(stored['result'])
        self.assertIsNone(stored['error'])
        self.assertEqual(self.handle(), 'not_started')
        self.adapter.run.assert_called_once()
