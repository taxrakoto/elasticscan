"""FastAPI boundary tests using real signed test JWTs and a test-only route."""
import json
import unittest
from unittest.mock import patch

from app.core.auth import get_token_validator
from app.main import app as production_app

from tests.auth_support import authentication_app, get
from tests.token_fixtures import TokenFixtures


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    async def test_valid_token_reaches_protected_handler(self):
        status, _, body = await get(authentication_app(self.tokens.validator()), 'Bearer ' + self.tokens.sign())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'subject': 'fixture-subject'})

    async def test_invalid_and_missing_credentials_return_uniform_401(self):
        credentials = {name: 'Bearer ' + token for name, token in self.tokens.invalid_tokens().items()}
        credentials.update(missing=None, basic='Basic Zm9vOmJhcg==', empty_bearer='Bearer ')
        app = authentication_app(self.tokens.validator())
        for name, authorization in credentials.items():
            with self.subTest(variant=name):
                status, headers, body = await get(app, authorization)
                self.assertEqual(status, 401)
                self.assertEqual(headers[b'www-authenticate'], b'Bearer')
                self.assertEqual(json.loads(body), {'detail': 'Invalid or missing access token'})

    async def test_dependency_declares_http_bearer_in_openapi(self):
        schema = authentication_app(self.tokens.validator()).openapi()
        self.assertEqual(schema['components']['securitySchemes']['BearerAuth']['scheme'], 'bearer')
        self.assertEqual(schema['paths']['/protected']['get']['security'], [{'BearerAuth': []}])

    async def test_startup_validates_configuration_without_fetching_keys(self):
        get_token_validator.cache_clear()
        self.addCleanup(get_token_validator.cache_clear)
        with patch.dict('os.environ', {
            'OIDC_ISSUER': self.tokens.settings.issuer,
            'OIDC_AUDIENCE': self.tokens.settings.audience,
            'OIDC_JWKS_URL': self.tokens.settings.jwks_url,
            'OIDC_ALGORITHMS': 'RS256',
        }, clear=True), patch('jwt.PyJWKClient.fetch_data') as fetch:
            async with production_app.router.lifespan_context(production_app):
                status, _, body = await get(production_app, path='/health/live')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['status'], 'ok')
            fetch.assert_not_called()

    async def test_missing_configuration_prevents_startup(self):
        get_token_validator.cache_clear()
        self.addCleanup(get_token_validator.cache_clear)
        with patch.dict('os.environ', {}, clear=True), self.assertRaises(ValueError):
            async with production_app.router.lifespan_context(production_app):
                self.fail('Application started without trust configuration')
