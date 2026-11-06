"""Test-only protected route and an ASGI request helper; no production endpoints."""
from typing import Annotated

from fastapi import Depends, FastAPI

from app.core.auth import get_token_validator, require_identity
from app.core.team import (
    TeamContext,
    get_team_settings,
    require_team_context,
)


def authentication_app(validator=None):
    app = FastAPI()
    if validator is not None:
        app.dependency_overrides[get_token_validator] = lambda: validator

    @app.get('/protected')
    def protected(claims: Annotated[dict, Depends(require_identity)]):
        return {'subject': claims['sub']}

    return app


def team_authorization_app(validator=None, settings=None):
    app = authentication_app(validator)
    app.state.team_handler_calls = []

    if settings is not None:
        app.dependency_overrides[get_team_settings] = lambda: settings

    @app.get('/team-protected')
    def protected_team(
        context: Annotated[TeamContext, Depends(require_team_context)],
    ):
        app.state.team_handler_calls.append(context)
        return {
            'team_id': context.team_id,
            'created_by': context.created_by,
        }

    return app


async def get(
    app, authorization=None, path='/protected', extra_headers=(),
):
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        messages.append(message)

    headers = [] if authorization is None else [(b'authorization', authorization.encode())]
    headers.extend(extra_headers)
    await app({
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
        'root_path': '', 'query_string': b'', 'headers': headers,
        'client': ('127.0.0.1', 12345), 'server': ('testserver', 80),
    }, receive, send)
    response = next(message for message in messages if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return response['status'], dict(response['headers']), body
