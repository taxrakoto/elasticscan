"""Ephemeral signing keys and tokens for security tests; never real credentials."""
import json
import time
from unittest.mock import Mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.auth import OIDCSettings, TokenValidator


class TokenFixtures:
    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.settings = OIDCSettings(
            issuer='https://issuer.example/realm', audience='elasticscan-api',
            jwks_url='https://issuer.example/keys', algorithms=('RS256',),
        )
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        jwk.update(kid='trusted-key', alg='RS256', use='sig')
        self.jwks = {'keys': [jwk]}

    def validator(self):
        validator = TokenValidator(self.settings)
        validator.jwks.fetch_data = Mock(return_value=self.jwks)
        return validator

    def claims(self, **changes):
        now = int(time.time())
        claims = {
            'iss': self.settings.issuer, 'aud': self.settings.audience,
            'sub': 'fixture-subject', 'exp': now + 300, 'iat': now - 1,
            'team_ids': ['team-alpha'],
        }
        claims.update(changes)
        return claims

    def sign(self, claims=None, *, key=None, headers=None, algorithm='RS256'):
        return jwt.encode(
            self.claims() if claims is None else claims,
            self.private_key if key is None else key,
            algorithm=algorithm,
            headers={'kid': 'trusted-key', **(headers or {})},
        )

    def invalid_tokens(self):
        now = int(time.time())
        variants = {
            'expired': self.sign(self.claims(exp=now - 10)),
            'future-not-before': self.sign(self.claims(nbf=now + 300)),
            'future-issued-at': self.sign(self.claims(iat=now + 300)),
            'wrong-issuer': self.sign(self.claims(iss='https://attacker.example')),
            'wrong-audience': self.sign(self.claims(aud='other-api')),
            'bad-signature': self.sign(key=self.other_key),
            'unknown-key': self.sign(headers={'kid': 'unknown-key'}),
            'missing-key-id': self.sign(headers={'kid': ''}),
            'symmetric-algorithm': self.sign(key='fixture-secret', algorithm='HS256'),
            'unsigned': self.sign(key='', algorithm='none'),
            'non-allowed-rsa-algorithm': self.sign(algorithm='RS512'),
            'malformed': 'not.a.jwt',
            'empty': '',
            'empty-subject': self.sign(self.claims(sub='  ')),
            'numeric-subject': self.sign(self.claims(sub=123)),
            'numeric-string-expiry': self.sign(self.claims(exp=str(now + 300))),
            'infinite-expiry': self.sign(self.claims(exp=float('inf'))),
            'null-not-before': self.sign(self.claims(nbf=None)),
        }
        for name in ('exp', 'iss', 'aud', 'sub'):
            claims = self.claims()
            del claims[name]
            variants[f'missing-{name}'] = self.sign(claims)
        return variants
