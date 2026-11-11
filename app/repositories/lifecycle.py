"""Guarded job transitions; callers own transaction commit and rollback."""
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import Job
from app.scanners.base import SeverityCounts


def _validate_worker(worker_id: str) -> None:
    if (
        not isinstance(worker_id, str)
        or not worker_id.strip()
        or worker_id != worker_id.strip()
    ):
        raise ValueError('worker_id must be a nonempty unpadded string')


def _lease_duration(lease_seconds: int) -> timedelta:
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError('lease_seconds must be a positive integer')
    try:
        return timedelta(seconds=lease_seconds)
    except OverflowError as exc:
        raise ValueError('lease_seconds is too large') from exc


def _locked_time(session: Session, job_id: UUID):
    """Lock the row before obtaining current database time."""
    found = session.scalar(
        select(Job.job_id)
        .where(Job.job_id == job_id)
        .with_for_update()
    )
    if found is None:
        return None
    return session.scalar(select(func.clock_timestamp()))


def claim_job(
    session: Session,
    *,
    job_id: UUID,
    worker_id: str,
    lease_seconds: int = 60,
) -> UUID | None:
    """Claim only pending work; return a token, durable after caller commit."""
    _validate_worker(worker_id)
    duration = _lease_duration(lease_seconds)
    now = _locked_time(session, job_id)
    if now is None:
        return None
    token = uuid4()
    return session.scalar(
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'pending',
            Job.cancellation_requested_at.is_(None),
            Job.claim_token.is_(None),
            Job.lease_expires_at.is_(None),
        )
        .values(
            status='running',
            worker_id=worker_id,
            claim_token=token,
            attempt_count=Job.attempt_count + 1,
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now + duration,
        )
        .returning(Job.claim_token)
        .execution_options(synchronize_session=False)
    )


def renew_job(
    session: Session,
    *,
    job_id: UUID,
    worker_id: str,
    claim_token: UUID,
    lease_seconds: int = 60,
) -> bool:
    """Extend a current unexpired claim; never revive an expired one."""
    _validate_worker(worker_id)
    duration = _lease_duration(lease_seconds)
    if not isinstance(claim_token, UUID):
        raise ValueError('claim_token must be a UUID')
    now = _locked_time(session, job_id)
    if now is None:
        return False
    return session.scalar(
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'running',
            Job.worker_id == worker_id,
            Job.claim_token == claim_token,
            Job.lease_expires_at > now,
        )
        .values(
            heartbeat_at=now,
            lease_expires_at=func.greatest(
                Job.lease_expires_at, now + duration,
            ),
        )
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    ) is not None


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
            Job.claim_token.is_(None),
            Job.lease_expires_at.is_(None),
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
    claim_token: UUID | None = None,
) -> bool:
    """Finish only an owned running job, checking any execution lease."""
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

    if claim_token is None:
        claim_guards = (
            Job.claim_token.is_(None),
            Job.lease_expires_at.is_(None),
        )
        finished_at = func.clock_timestamp()
    else:
        if not isinstance(claim_token, UUID):
            raise ValueError('claim_token must be a UUID')
        finished_at = _locked_time(session, job_id)
        if finished_at is None:
            return False
        claim_guards = (
            Job.claim_token == claim_token,
            Job.lease_expires_at > finished_at,
        )

    statement = (
        update(Job)
        .where(
            Job.job_id == job_id,
            Job.status == 'running',
            Job.worker_id == worker_id,
            *claim_guards,
        )
        .values(
            status=status,
            result=result,
            error=error,
            finished_at=finished_at,
            claim_token=None,
            lease_expires_at=None,
        )
        .returning(Job.job_id)
        .execution_options(synchronize_session=False)
    )
    return session.scalar(statement) is not None


def complete_job(
    session: Session,
    *,
    job_id: UUID,
    worker_id: str,
    claim_token: UUID,
    counts: SeverityCounts,
) -> bool:
    """Save validated counts and completion; caller owns commit/rollback."""
    if not isinstance(claim_token, UUID):
        raise ValueError('claim_token must be a UUID')
    if not isinstance(counts, SeverityCounts):
        raise ValueError('counts must be SeverityCounts')
    result = asdict(counts)
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise ValueError('Severity counts must be nonnegative integers')
    return finish_job(
        session,
        job_id=job_id,
        worker_id=worker_id,
        claim_token=claim_token,
        status='completed',
        result=result,
        error=None,
    )


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
