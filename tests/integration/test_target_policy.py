"""Authenticated initial-target checks through a test-only HTTP route."""
import json
import unittest
from typing import Annotated
from unittest.mock import Mock, patch

from fastapi import Depends, FastAPI

from app.core.auth import get_token_validator
from app.core.target_policy import (
    TargetPolicy,
    get_target_policy,
    require_allowed_submission,
)
from app.main import app as production_app
from app.schemas import ZapSubmission
from tests.integration.test_submission_validation import post_json
from tests.token_fixtures import TokenFixtures


class TargetPolicyHTTPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    def application(self, resolver):
        app = FastAPI()
        app.state.submissions = []
        policy = TargetPolicy(['https://approved.example.invalid'], resolver)
        app.dependency_overrides[get_token_validator] = (
            lambda: self.tokens.validator()
        )
        app.dependency_overrides[get_target_policy] = lambda: policy

        @app.post('/scan')
        def submit(
            submission: Annotated[
                ZapSubmission, Depends(require_allowed_submission),
            ],
        ):
            app.state.submissions.append(submission)
            return {'allowed': True}

        return app

    def payload(self, url='https://approved.example.invalid/'):
        return {'scan_type': 'zap', 'parameters': {'url': url}}

    async def test_authorized_public_target_reaches_handler(self):
        resolver = Mock(return_value=('8.8.8.8',))
        app = self.application(resolver)
        status, body = await post_json(
            app, self.payload(), 'Bearer ' + self.tokens.sign(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'allowed': True})
        self.assertEqual(len(app.state.submissions), 1)
        resolver.assert_called_once()

    async def test_target_denial_is_uniform_and_stops_handler(self):
        for url, answers in (
            ('https://other.example.invalid/', ('8.8.8.8',)),
            ('https://approved.example.invalid/', ('127.0.0.1',)),
            ('https://approved.example.invalid/', ('8.8.8.8', '::1')),
        ):
            with self.subTest(url=url, answers=answers):
                resolver = Mock(return_value=answers)
                app = self.application(resolver)
                status, body = await post_json(
                    app, self.payload(url), 'Bearer ' + self.tokens.sign(),
                )
                self.assertEqual(status, 403)
                self.assertEqual(
                    json.loads(body), {'detail': 'Target is not allowed'},
                )
                self.assertEqual(app.state.submissions, [])

    async def test_authentication_and_team_denials_do_not_resolve_dns(self):
        resolver = Mock(return_value=('8.8.8.8',))
        app = self.application(resolver)
        cases = (
            (None, 401),
            ('Bearer malformed', 401),
            (
                'Bearer ' + self.tokens.sign(self.tokens.claims(team_ids=[])),
                403,
            ),
        )
        for authorization, expected in cases:
            status, _ = await post_json(app, self.payload(), authorization)
            self.assertEqual(status, expected)
        resolver.assert_not_called()
        self.assertEqual(app.state.submissions, [])

    async def test_invalid_url_remains_422_without_dns(self):
        resolver = Mock()
        app = self.application(resolver)
        for value in ('not-a-url', 'ftp://approved.example.invalid/'):
            status, _ = await post_json(
                app, self.payload(value), 'Bearer ' + self.tokens.sign(),
            )
            self.assertEqual(status, 422)
        resolver.assert_not_called()
        self.assertEqual(app.state.submissions, [])

    async def test_invalid_configuration_prevents_startup_without_dns(self):
        with patch('app.main.get_token_validator'):
            with patch('app.main.get_team_settings'):
                with patch.dict(
                    'os.environ', {'TARGET_ALLOWED_ORIGINS': '*'}, clear=True,
                ):
                    with patch('socket.getaddrinfo') as lookup:
                        with self.assertRaises(ValueError):
                            async with production_app.router.lifespan_context(
                                production_app,
                            ):
                                self.fail('Invalid target policy was accepted')
                        lookup.assert_not_called()
