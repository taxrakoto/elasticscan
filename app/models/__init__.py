"""Register persisted entities in the shared SQLAlchemy metadata."""
from app.models.job import Job
from app.models.dispatch import DispatchIntent

__all__ = ['Job', 'DispatchIntent']
