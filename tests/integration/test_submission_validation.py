"""Pydantic/FastAPI boundary tests, independent of the production /scan route.

The test-only handler is a spy for whether request validation admitted input.
These tests do not simulate job persistence or claim durable job acceptance.
"""
import json
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, Response

from app.schemas import ZapSubmission
from tests.submission_contract import examples


def validation_app(handler):
    app = FastAPI()

    @app.post('/scan', status_code=202)
    async def submit(body: ZapSubmission):
        handler(body)
        return Response(status_code=202)

    return app


async def post_json(
    app, payload, authorization=None, extra_headers=(),
):
    """Call the real ASGI application without sockets or an extra client library."""
    messages = []
    body = json.dumps(payload).encode()
    headers = [(b'content-type', b'application/json')]
    if authorization is not None:
        headers.append((b'authorization', authorization.encode()))
    headers.extend(extra_headers)

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    async def send(message):
        messages.append(message)

    await app({
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'POST', 'scheme': 'http', 'path': '/scan', 'raw_path': b'/scan',
        'root_path': '', 'query_string': b'',
        'headers': headers,
        'client': ('127.0.0.1', 12345), 'server': ('testserver', 80),
    }, receive, send)
    status = next(message['status'] for message in messages if message['type'] == 'http.response.start')
    response_body = b''.join(message.get('body', b'') for message in messages
                             if message['type'] == 'http.response.body')
    return status, response_body


class SubmissionValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_input_reaches_the_handler_as_a_typed_submission(self):
        for example in examples()['valid']:
            with self.subTest(example=example['name']):
                handler = Mock()
                app = validation_app(handler)

                status, _ = await post_json(app, example['payload'])

                self.assertEqual(status, 202)
                handler.assert_called_once()
                submission = handler.call_args.args[0]
                self.assertIsInstance(submission, ZapSubmission)
                self.assertEqual(submission.model_dump(mode='json'), example['payload'])

    async def test_invalid_input_returns_structured_422_before_the_handler_runs(self):
        for example in examples()['invalid']:
            with self.subTest(example=example['name']):
                handler = Mock()
                app = validation_app(handler)

                status, body = await post_json(app, example['payload'])

                self.assertEqual(status, 422)
                handler.assert_not_called()
                detail = json.loads(body)['detail']
                self.assertIsInstance(detail, list)
                self.assertTrue(detail)
                expected = ['body', *example['error_location']]
                matching = [error for error in detail if error['loc'] == expected]
                self.assertTrue(matching, f'Expected an HTTP validation error at {expected}')
                for error in matching:
                    self.assertIsInstance(error['msg'], str)
                    self.assertTrue(error['msg'])
                    self.assertIsInstance(error['type'], str)
                    self.assertTrue(error['type'])


if __name__ == '__main__':
    unittest.main()
