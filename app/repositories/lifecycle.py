"""Guarded job transitions; callers own transaction commit and rollback."""
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import Job


def _validate_worker(worker_id: str) -> None:
    if (
        not isinstance(worker_id, str)
        or not worker_id.strip()
        or worker_id != worker_id.strip()
    ):
        raise ValueError('worker_id must be a nonempty unpadded string')


def start_job(
    session: Session,
    *,
    job_id: UUID,
    worker_id: str,
) -> bool:
    """Start only a pending job, recording its execution owner."""
    _validate_worker(worker_id)
    statement = (
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'pending',
            Job.cancellation_requested_at.is_(None),
        )
        .values(
            status='running',
            worker_id=worker_id,
            attempt_count=Job.attempt_count + 1,
            started_at=func.statement_timestamp(),
        )
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    )
    return session.scalar(statement) is not None


def finish_job(
    session: Session,
    *,
    job_id: UUID,
    worker_id: str,
    status: Literal['completed', 'failed'],
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> bool:
    """Finish only a running job owned by this worker."""
    _validate_worker(worker_id)
    if status == 'completed':
        valid = isinstance(result, dict) and error is None
    elif status == 'failed':
        valid = isinstance(error, dict) and result is None
    else:
        valid = False
    if not valid:
        raise ValueError(
            'Completion requires only a result object; '
            'failure requires only an error object'
        )

    statement = (
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'running',
            Job.worker_id == worker_id,
        )
        .values(
            status=status,
            result=result,
            error=error,
            finished_at=func.statement_timestamp(),
        )
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    )
    return session.scalar(statement) is not None


def fail_pending_job(
    session: Session,
    *,
    job_id: UUID,
    error: dict[str, Any],
) -> bool:
    """Record exhausted dispatch/recovery failure before execution starts."""
    if not isinstance(error, dict):
        raise ValueError('Failure requires an error object')

    statement = (
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'pending',
        )
        .values(
            status='failed',
            result=None,
            error=error,
            finished_at=func.statement_timestamp(),
        )
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    )
    return session.scalar(statement) is not None
