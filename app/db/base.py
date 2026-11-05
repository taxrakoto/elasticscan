"""Shared metadata for SQLAlchemy models and Alembic."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
