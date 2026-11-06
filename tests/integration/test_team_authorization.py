"""Team authorization through FastAPI and verified signed JWTs."""
import json
import unittest
from unittest.mock import patch

from app.core.team import TeamSettings
from app.main import app as production_app
from tests.auth_support import get, team_authorization_app
from tests.token_fixtures import TokenFixtures


class TeamAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    def application(self, settings=None):
        return team_authorization_app(
            self.tokens.validator(), settings or TeamSettings(),
        )

    async def request(self, app, claims, selectors=()):
        return await get(
            app,
            'Bearer ' + self.tokens.sign(claims),
            path='/team-protected',
            extra_headers=[
                (b'x-team-id', value.encode()) for value in selectors
            ],
        )

    async def test_members_resolve_to_their_authorized_team(self):
        app = self.application()
        cases = (
            ('alice', ['team-alpha'], (), 'team-alpha'),
            ('alice-peer', ['team-alpha'], (), 'team-alpha'),
            ('bob', ['team-beta'], (), 'team-beta'),
            ('multi', ['team-alpha', 'team-beta'], ('team-beta',), 'team-beta'),
        )
        for subject, memberships, selectors, expected in cases:
            with self.subTest(subject=subject):
                status, _, body = await self.request(
                    app,
                    self.tokens.claims(sub=subject, team_ids=memberships),
                    selectors,
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    json.loads(body),
                    {'team_id': expected, 'created_by': subject},
                )
        self.assertEqual(len(app.state.team_handler_calls), 4)

    async def test_invalid_context_never_executes_handler(self):
        app = self.application()
        missing = self.tokens.claims()
        del missing['team_ids']
        cases = [
            (missing, ()),
            (self.tokens.claims(team_ids=[]), ()),
            (self.tokens.claims(team_ids='team-alpha'), ()),
            (self.tokens.claims(team_ids=['team-alpha', None]), ()),
            (self.tokens.claims(team_ids=['team-alpha', 'team-beta']), ()),
            (self.tokens.claims(), ('team-beta',)),
            (self.tokens.claims(), ('',)),
            (self.tokens.claims(), (' ',)),
            (self.tokens.claims(), ('team-alpha', 'team-alpha')),
            (self.tokens.claims(), ('team-alpha', 'team-beta')),
            (self.tokens.claims(), ('team-alpha,team-beta',)),
        ]
        for claims, selectors in cases:
            with self.subTest(claims=claims, selectors=selectors):
                status, _, body = await self.request(app, claims, selectors)
                self.assertEqual(status, 403)
                self.assertEqual(
                    json.loads(body),
                    {'detail': 'Missing or unauthorized team context'},
                )
        self.assertEqual(app.state.team_handler_calls, [])

    async def test_invalid_identity_remains_401(self):
        app = self.application()
        credentials = [
            None,
            'Basic Zm9vOmJhcg==',
            *(
                'Bearer ' + token
                for token in self.tokens.invalid_tokens().values()
            ),
        ]
        for authorization in credentials:
            status, headers, body = await get(
                app, authorization, path='/team-protected',
                extra_headers=[(b'x-team-id', b'team-alpha')],
            )
            self.assertEqual(status, 401)
            self.assertEqual(headers[b'www-authenticate'], b'Bearer')
            self.assertEqual(
                json.loads(body),
                {'detail': 'Invalid or missing access token'},
            )
        self.assertEqual(app.state.team_handler_calls, [])

    async def test_configured_mapping_is_used_at_http_boundary(self):
        app = self.application(TeamSettings('memberships'))
        status, _, body = await self.request(
            app,
            self.tokens.claims(
                memberships=['team-beta'], team_ids=['team-alpha'],
            ),
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['team_id'], 'team-beta')

    async def test_openapi_declares_bearer_and_optional_team_header(self):
        operation = self.application().openapi()['paths'][
            '/team-protected'
        ]['get']
        self.assertEqual(operation['security'], [{'BearerAuth': []}])
        parameter = next(
            item for item in operation['parameters']
            if item['name'] == 'X-Team-ID'
        )
        self.assertEqual(parameter['in'], 'header')
        self.assertFalse(parameter['required'])

    async def test_invalid_team_configuration_prevents_startup(self):
        with patch('app.main.get_token_validator'):
            with patch.dict(
                'os.environ', {'OIDC_TEAM_CLAIM': ''}, clear=True,
            ):
                with self.assertRaises(ValueError):
                    async with production_app.router.lifespan_context(
                        production_app,
                    ):
                        self.fail('Invalid team configuration was accepted')
