"""US-003.1 request behavior: FR-001/FR-002 and the schema portion of AC-002."""
import json
import unittest

from pydantic import ValidationError

from tests.submission_contract import examples, submission_model


class SubmissionSchemaTests(unittest.TestCase):
    def test_accepts_valid_http_and_https_zap_submissions(self):
        model = submission_model(self)
        for example in examples()['valid']:
            with self.subTest(example=example['name']):
                submission = model.model_validate(example['payload'])

                self.assertEqual(submission.scan_type, 'zap')
                self.assertEqual(str(submission.parameters.url), example['payload']['parameters']['url'])

    def test_serializes_valid_submissions_to_the_public_json_shape(self):
        model = submission_model(self)
        for example in examples()['valid']:
            with self.subTest(example=example['name']):
                submission = model.model_validate(example['payload'])

                self.assertEqual(json.loads(submission.model_dump_json()), example['payload'])

    def test_rejects_invalid_submissions_with_field_level_details(self):
        model = submission_model(self)
        for example in examples()['invalid']:
            with self.subTest(example=example['name']):
                with self.assertRaises(ValidationError) as raised:
                    model.model_validate(example['payload'])

                matching = [error for error in raised.exception.errors()
                            if list(error['loc']) == example['error_location']]
                self.assertTrue(matching, f"Expected a validation error at {example['error_location']}")
                for error in matching:
                    self.assertIsInstance(error['msg'], str)
                    self.assertTrue(error['msg'])
                    self.assertIsInstance(error['type'], str)
                    self.assertTrue(error['type'])


if __name__ == '__main__':
    unittest.main()
