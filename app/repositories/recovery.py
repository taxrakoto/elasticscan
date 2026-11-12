"""Conservative terminal recovery for abandoned execution claims."""
from sqlalchemy import func, select

from app.core.failure import safe_error
from app.models import Job


def fail_expired_claims(session, *, limit=10):
    """Fail expired ZAP claims; caller owns transaction commit/rollback."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Recovery limit must be between 1 and 100')

    jobs = list(session.scalars(
        select(Job)
        .where(
            Job.scan_type == 'zap',
            Job.status == 'running',
            Job.claim_token.is_not(None),
            Job.lease_expires_at <= func.clock_timestamp(),
            Job.cancellation_requested_at.is_(None),
        )
        .order_by(Job.lease_expires_at, Job.job_id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ))

    recovered = []
    for job in jobs:
        now = session.scalar(select(func.clock_timestamp()))
        if job.lease_expires_at > now:
            continue
        job.status = 'failed'
        job.result = None
        job.error = safe_error('recovery_exhausted')
        job.finished_at = now
        job.claim_token = None
        job.lease_expires_at = None
        recovered.append(job.job_id)

    session.flush()
    return recovered
