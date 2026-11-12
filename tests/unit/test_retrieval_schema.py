"""Public retrieval schema behavior without external services."""
import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.core.failure import (
    EXHAUSTED_FAILURE_CODES, SCANNER_FAILURE_CODES, safe_error,
)
from app.schemas.retrieval import JobStatusResponse
from tests.retrieval_contract import (
    PUBLIC_FIELDS, TIMESTAMP_FIELDS, retrieval_example,
)


class RetrievalSchemaTests(unittest.TestCase):
    def test_five_states_keep_null_fields_and_serialize_utc(self):
        for state in (
            'pending', 'running', 'completed', 'failed', 'cancelled',
        ):
            with self.subTest(state=state):
                source = retrieval_example(state)
                response = JobStatusResponse.model_validate(source)
                body = json.loads(response.model_dump_json())
                self.assertEqual(set(body), PUBLIC_FIELDS)
                self.assertEqual(body['status'], state)
                for field in TIMESTAMP_FIELDS:
                    if source[field] is None:
                        self.assertIsNone(body[field])
                    else:
                        parsed = datetime.fromisoformat(
                            body[field].replace('Z', '+00:00')
                        )
                        self.assertEqual(parsed, source[field])
                        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
                for field in ('result', 'error'):
                    self.assertEqual(body[field], source[field])

    def test_inconsistent_states_are_rejected(self):
        now = datetime.now(timezone.utc)
        cases = (
            ('pending', {'started_at': now}),
            ('pending', {'finished_at': now}),
            ('running', {'started_at': None}),
            ('completed', {'result': None}),
            ('completed', {'started_at': None}),
            ('failed', {'error': None}),
            ('failed', {'finished_at': None}),
            ('cancelled', {'cancelled_at': None}),
            ('cancelled', {'cancellation_requested_by': None}),
            ('running', {'cancelled_at': now}),
            ('pending', {'cancellation_requested_by': 'alice'}),
            ('pending', {'created_at': datetime(2026, 11, 12)}),
        )
        for state, changes in cases:
            with self.subTest(state=state, changes=changes):
                data = retrieval_example(state)
                data.update(changes)
                with self.assertRaises(ValidationError):
                    JobStatusResponse.model_validate(data)

    def test_counts_require_nonnegative_integers(self):
        for value in (-1, True, 1.5, '1'):
            data = retrieval_example('completed')
            data['result']['high'] = value
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    JobStatusResponse.model_validate(data)

    def test_errors_are_reconstructed_from_the_safe_catalog(self):
        for code in SCANNER_FAILURE_CODES | EXHAUSTED_FAILURE_CODES:
            data = retrieval_example('failed')
            data['error'] = {
                'code': code,
                'message': 'Traceback password=secret',
                'report': 'Bearer secret-token',
            }
            response = JobStatusResponse.model_validate(data)
            self.assertEqual(response.error.model_dump(), safe_error(code))
            self.assertNotIn('secret', response.model_dump_json())

        data['error'] = {'code': 'unknown', 'message': 'secret'}
        with self.assertRaises(ValidationError):
            JobStatusResponse.model_validate(data)

    def test_pending_cancellation_and_failure_before_start_are_supported(self):
        data = retrieval_example('running')
        data.update(
            cancellation_requested_by='alice',
            cancellation_requested_at=data['started_at'],
        )
        self.assertEqual(
            JobStatusResponse.model_validate(data).status, 'running',
        )
        data = retrieval_example('failed')
        data['started_at'] = None
        self.assertIsNone(
            JobStatusResponse.model_validate(data).started_at,
        )
