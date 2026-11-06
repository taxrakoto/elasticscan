"""JWT verification using deployment-configured trust, never token-supplied URLs."""
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...]

    def __post_init__(self):
        for value in (self.issuer, self.jwks_url):
            parsed = urlsplit(value)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('OIDC issuer and JWKS URL must be configured HTTP(S) URLs')
        if not self.audience.strip():
            raise ValueError('OIDC_AUDIENCE must not be empty')
        # Only asymmetric RSA signatures are supported by this deployment.
        # In particular, never mix HS* secrets with public signing keys.
        if not self.algorithms or not set(self.algorithms) <= {'RS256', 'RS384', 'RS512'}:
            raise ValueError('OIDC_ALGORITHMS must explicitly allow RSA signing algorithms')

    @classmethod
    def from_environment(cls):
        return cls(
            issuer=os.environ.get('OIDC_ISSUER', ''),
            audience=os.environ.get('OIDC_AUDIENCE', ''),
            jwks_url=os.environ.get('OIDC_JWKS_URL', ''),
            algorithms=tuple(value.strip() for value in os.environ.get('OIDC_ALGORITHMS', '').split(',') if value.strip()),
        )


class TokenValidator:
    def __init__(self, settings: OIDCSettings):
        self.settings = settings
        self.jwks = jwt.PyJWKClient(
            settings.jwks_url, cache_jwk_set=True, lifespan=300,
            cache_keys=False, timeout=5,
        )

    def validate(self, token: str) -> dict[str, Any]:
        """Return claims only after all checks; failures expose no token details."""
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get('alg')
            kid = header.get('kid')
            if not isinstance(algorithm, str) or algorithm not in self.settings.algorithms:
                raise jwt.InvalidTokenError('Disallowed signing algorithm')
            if not isinstance(kid, str) or not kid.strip():
                raise jwt.InvalidTokenError('Missing signing key identifier')
            # The kid only selects a key at the configured endpoint. Ignore jku,
            # x5u, embedded jwk, and any issuer advertised by unverified claims.
            signing_key = self.jwks.get_signing_key(kid)
            if signing_key.algorithm_name != algorithm:
                raise jwt.InvalidTokenError('Signing key algorithm mismatch')
            claims = jwt.decode(
                token, signing_key.key,
                algorithms=list(self.settings.algorithms),
                issuer=self.settings.issuer, audience=self.settings.audience,
                options={'require': ['iss', 'aud', 'exp', 'sub']},
                leeway=0,
            )
            if not isinstance(claims['sub'], str) or not claims['sub'].strip():
                raise jwt.InvalidTokenError('Missing subject')
            for name in ('exp', 'nbf', 'iat'):
                if name in claims:
                    value = claims[name]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise jwt.InvalidTokenError('Invalid token lifetime')
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise jwt.InvalidTokenError('Invalid access token') from None


@lru_cache(maxsize=1)
def get_token_validator() -> TokenValidator:
    """One bounded JWKS cache per process, shared by protected routes."""
    return TokenValidator(OIDCSettings.from_environment())


bearer = HTTPBearer(auto_error=False, scheme_name='BearerAuth', description='API access token from the configured trusted issuer')


def require_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    validator: Annotated[TokenValidator, Depends(get_token_validator)],
) -> dict[str, Any]:
    """Return verified claims for authentication and team authorization."""
    if credentials is not None:
        try:
            return validator.validate(credentials.credentials)
        except jwt.InvalidTokenError:
            pass
    raise HTTPException(
        status_code=401, detail='Invalid or missing access token',
        headers={'WWW-Authenticate': 'Bearer'},
    )
