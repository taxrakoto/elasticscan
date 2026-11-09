"""Job persistence using validated submissions and authorized ownership."""
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.team import TeamContext
from app.models import Job
from app.schemas import ZapSubmission


def create_pending_job(
    session: Session,
    *,
    submission: ZapSubmission,
    context: TeamContext,
) -> Job:
    """Insert a job; the caller owns commit/rollback and dispatch intent.

    The context must come from verified team authorization, never from
    request JSON. This function does not authenticate callers.
    """
    job = Job(
        team_id=context.team_id,
        created_by=context.created_by,
        scan_type=submission.scan_type,
        parameters=submission.parameters.model_dump(mode='json'),
    )
    session.add(job)
    session.flush()
    return job


def find_team_job(
    session: Session,
    *,
    job_id: UUID,
    context: TeamContext,
) -> Job | None:
    """Read only within a verified team; caller owns the session."""
    return session.scalar(
        select(Job).where(
            Job.job_id == job_id,
            Job.team_id == context.team_id,
        )
    )
