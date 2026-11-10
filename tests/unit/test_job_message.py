"""Shared message-contract validation without external services."""
import json
import unittest
from copy import deepcopy
from pathlib import Path

from pydantic import ValidationError

from app.schemas.job_message import JobMessage


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / 'fixtures/contracts/v1/job-message.json'
)


class JobMessageTests(unittest.TestCase):
    def test_version_one_fixture_round_trips(self):
        payload = json.loads(FIXTURE.read_text())
        message = JobMessage.model_validate(payload)
        self.assertEqual(json.loads(message.model_dump_json()), payload)

    def test_invalid_messages_are_rejected(self):
        original = json.loads(FIXTURE.read_text())
        changes = (
            ('schema_version', 2),
            ('job_id', 'invalid'),
            ('scan_type', 'unrelated'),
            ('parameters', {'url': 'file:///etc/passwd'}),
            ('parameters', {
                'url': 'https://approved.example.invalid/',
                'command': 'unexpected',
            }),
            ('team_id', 'untrusted-team'),
        )
        for field, value in changes:
            with self.subTest(field=field, value=value):
                payload = deepcopy(original)
                payload[field] = value
                with self.assertRaises(ValidationError):
                    JobMessage.model_validate(payload)
        for field in original:
            with self.subTest(missing=field):
                payload = deepcopy(original)
                del payload[field]
                with self.assertRaises(ValidationError):
                    JobMessage.model_validate(payload)
