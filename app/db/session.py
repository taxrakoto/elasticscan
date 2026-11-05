"""Explicit database factories; importing this module opens no connections."""
import os

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker


def database_url() -> URL:
    """Use the same libpq environment settings as the local Compose stack."""
    return URL.create(
        'postgresql+psycopg',
        username=os.environ.get('PGUSER'),
        password=os.environ.get('PGPASSWORD'),
        host=os.environ.get('PGHOST'),
        port=int(os.environ.get('PGPORT', '5432')),
        database=os.environ.get('PGDATABASE'),
    )


def make_engine():
    return create_engine(
        database_url(), pool_pre_ping=True,
        connect_args={'options': '-c timezone=UTC'},
    )


def make_session_factory(engine):
    """Callers own transaction boundaries, usually with Session.begin()."""
    return sessionmaker(bind=engine, expire_on_commit=False)
