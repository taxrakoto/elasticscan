"""Alembic uses runtime credentials, never a password in alembic.ini."""
from alembic import context

from app.db.base import Base
from app.db.session import database_url, make_engine
from app import models  # noqa: F401 -- register tables for autogeneration

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=database_url(), target_metadata=Base.metadata,
        literal_binds=True, dialect_opts={'paramstyle': 'named'},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get('connection') is not None:
    # Allows isolated integration tests to supply their own connection.
    run(config.attributes['connection'])
else:
    engine = make_engine()
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
