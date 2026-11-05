"""Opt-in integration with the configured development issuer and actual JWKS."""
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from app.core.auth import OIDCSettings, TokenValidator, get_token_validator
from tests.auth_support import authentication_app, get


@unittest.skipUnless(os.environ.get('RUN_KEYCLOAK_TESTS') == '1', 'Set RUN_KEYCLOAK_TESTS=1 for local Keycloak tests')
class KeycloakAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = OIDCSettings.from_environment()
        # Explicit local-only fixtures; never send development passwords elsewhere.
        if cls.settings.issuer != 'http://localhost:8080/realms/elasticscan' or cls.settings.jwks_url != 'http://keycloak:8080/realms/elasticscan/protocol/openid-connect/certs':
            raise RuntimeError('Keycloak fixture tests require the default local issuer and JWKS endpoint')
        cls.tokens = {}
        for username in ('alice', 'alice-peer', 'bob'):
            data = urlencode({
                'grant_type': 'password', 'client_id': 'elasticscan-dev',
                'username': username, 'password': os.environ['LOCAL_USER_PASSWORD'],
            }).encode()
            request = Request('http://keycloak:8080/realms/elasticscan/protocol/openid-connect/token', data=data)
            with urlopen(request, timeout=10) as response:
                cls.tokens[username] = json.load(response)['access_token']
        get_token_validator.cache_clear()

    @classmethod
    def tearDownClass(cls):
        get_token_validator.cache_clear()

    async def test_real_access_tokens_authenticate_using_configured_dependency(self):
        app = authentication_app()
        for username, token in self.tokens.items():
            with self.subTest(user=username):
                status, _, body = await get(app, 'Bearer ' + token)
                self.assertEqual(status, 200)
                claims = get_token_validator().validate(token)
                self.assertEqual(json.loads(body)['subject'], claims['sub'])
                expected_team = 'team-beta' if username == 'bob' else 'team-alpha'
                self.assertEqual(claims['team_ids'], [expected_team])

    async def test_real_token_rejected_for_wrong_trust_or_signature(self):
        token = self.tokens['alice']
        for changes in ({'issuer': 'https://untrusted.example'}, {'audience': 'other-api'}):
            with self.subTest(changes=changes):
                validator = TokenValidator(replace(self.settings, **changes))
                status, _, _ = await get(authentication_app(validator), 'Bearer ' + token)
                self.assertEqual(status, 401)
        prefix, signature = token.rsplit('.', 1)
        corrupt = prefix + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:]
        app = authentication_app()
        for authorization in ('Bearer ' + corrupt, None):
            status, _, _ = await get(app, authorization)
            self.assertEqual(status, 401)

    async def test_real_expired_token_is_rejected_without_waiting(self):
        token = self.tokens['alice']
        expiry = get_token_validator().validate(token)['exp']
        # Advance only the verifier's clock; preserve the provider's signature.
        with patch('jwt.api_jwt.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.fromtimestamp(expiry + 1, timezone.utc)
            status, headers, _ = await get(authentication_app(), 'Bearer ' + token)
        self.assertEqual(status, 401)
        self.assertEqual(headers[b'www-authenticate'], b'Bearer')
