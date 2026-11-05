"""Cryptographic validation tests with generated RSA keys, without network."""
import unittest
from dataclasses import replace
from unittest.mock import patch

import jwt

from app.core.auth import OIDCSettings, TokenValidator
from tests.token_fixtures import TokenFixtures


class TokenValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = TokenFixtures()

    def test_accepts_valid_api_token_and_past_optional_not_before(self):
        validator = self.tokens.validator()
        for changes in ({}, {'nbf': 1}, {'aud': ['other-api', 'elasticscan-api']}):
            with self.subTest(changes=changes):
                claims = validator.validate(self.tokens.sign(self.tokens.claims(**changes)))
                self.assertEqual(claims['sub'], 'fixture-subject')
                self.assertEqual(claims['team_ids'], ['team-alpha'])

    def test_rejects_each_invalid_token_variant(self):
        for name, token in self.tokens.invalid_tokens().items():
            with self.subTest(variant=name), self.assertRaises(jwt.InvalidTokenError):
                self.tokens.validator().validate(token)

    def test_rejects_disallowed_algorithms_before_fetching_keys(self):
        validator = self.tokens.validator()
        for algorithm, key in (('HS256', 'fixture-secret'), ('none', ''), ('RS512', None)):
            with self.subTest(algorithm=algorithm), self.assertRaises(jwt.InvalidTokenError):
                validator.validate(self.tokens.sign(algorithm=algorithm, key=key))
        validator.jwks.fetch_data.assert_not_called()

    def test_token_headers_cannot_replace_the_trusted_key_source(self):
        validator = self.tokens.validator()
        token = self.tokens.sign(headers={
            'jku': 'https://attacker.example/keys',
            'x5u': 'https://attacker.example/certificate',
            'jwk': {'kty': 'oct', 'k': 'attacker-key'},
        })
        self.assertEqual(validator.validate(token)['sub'], 'fixture-subject')
        self.assertEqual(validator.jwks.uri, self.tokens.settings.jwks_url)
        with self.assertRaises(jwt.InvalidTokenError):
            validator.validate(self.tokens.sign(key=self.tokens.other_key, headers={'jku': 'https://attacker.example/keys'}))

    def test_key_fetch_failures_and_malformed_sets_fail_closed(self):
        for response in ({'keys': []}, {'keys': 'invalid'}, [], {'keys': [{'kty': 'unknown'}]}):
            with self.subTest(response=response), self.assertRaises(jwt.InvalidTokenError):
                validator = self.tokens.validator()
                validator.jwks.fetch_data.return_value = response
                validator.validate(self.tokens.sign())
        validator = self.tokens.validator()
        validator.jwks.fetch_data.side_effect = jwt.PyJWKClientConnectionError('internal endpoint unavailable')
        with self.assertRaisesRegex(jwt.InvalidTokenError, '^Invalid access token$'):
            validator.validate(self.tokens.sign())

    def test_requires_explicit_trust_configuration(self):
        for changes in (
            {'issuer': ''}, {'audience': ''}, {'jwks_url': ''},
            {'jwks_url': 'file:///tmp/keys'}, {'algorithms': ()},
            {'algorithms': ('none',)}, {'algorithms': ('RS256', 'HS256')},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.tokens.settings, **changes)
        with patch.dict('os.environ', {
            'OIDC_ISSUER': self.tokens.settings.issuer,
            'OIDC_AUDIENCE': self.tokens.settings.audience,
            'OIDC_JWKS_URL': self.tokens.settings.jwks_url,
            'OIDC_ALGORITHMS': 'RS256',
        }, clear=True):
            self.assertEqual(OIDCSettings.from_environment(), self.tokens.settings)
