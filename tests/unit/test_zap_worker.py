"""Invalid messages must not reach storage or a scanner."""
import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from app.workers.zap import handle_message


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / 'fixtures/contracts/v1/job-message.json'
)


class ZapWorkerValidationTests(unittest.TestCase):
    def test_invalid_messages_never_access_storage_or_adapter(self):
        original = json.loads(FIXTURE.read_text())
        bodies = ['{', 'null', '[]', '"text"']

        for field, value in (
            ('schema_version', 2),
            ('schema_version', True),
            ('schema_version', 1.0),
            ('schema_version', '1'),
            ('job_id', 'invalid'),
            ('scan_type', 'other'),
            ('parameters', {'url': 'file:///etc/passwd'}),
            ('parameters', {
                'url': original['parameters']['url'],
                'command': 'unexpected',
            }),
            ('team_id', 'untrusted-team'),
        ):
            payload = deepcopy(original)
            payload[field] = value
            bodies.append(json.dumps(payload))

        for field in original:
            payload = deepcopy(original)
            del payload[field]
            bodies.append(json.dumps(payload))

        for body in bodies:
            with self.subTest(body=body):
                sessions = Mock()
                adapter = Mock()
                decision = handle_message(
                    body,
                    sessions=sessions,
                    worker_id='worker-a',
                    adapter=adapter,
                )
                self.assertEqual(decision, 'invalid_message')
                sessions.begin.assert_not_called()
                adapter.run.assert_not_called()
