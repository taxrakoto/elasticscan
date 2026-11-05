"""Test-only protected route and an ASGI request helper; no production endpoints."""
from typing import Annotated

from fastapi import Depends, FastAPI

from app.core.auth import get_token_validator, require_identity


def authentication_app(validator=None):
    app = FastAPI()
    if validator is not None:
        app.dependency_overrides[get_token_validator] = lambda: validator

    @app.get('/protected')
    def protected(claims: Annotated[dict, Depends(require_identity)]):
        return {'subject': claims['sub']}

    return app


async def get(app, authorization=None, path='/protected'):
    messages = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        messages.append(message)

    headers = [] if authorization is None else [(b'authorization', authorization.encode())]
    await app({
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
        'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
        'root_path': '', 'query_string': b'', 'headers': headers,
        'client': ('127.0.0.1', 12345), 'server': ('testserver', 80),
    }, receive, send)
    response = next(message for message in messages if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in messages if message['type'] == 'http.response.body')
    return response['status'], dict(response['headers']), body
