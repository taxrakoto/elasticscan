"""Supply the application's database session factory to HTTP routes."""
from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker


def get_session_factory(request: Request) -> sessionmaker[Session]:
    return request.app.state.sessions
