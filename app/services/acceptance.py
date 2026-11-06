"""Commit a pending job and its dispatch intent as one operation."""
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.core.team import TeamContext
from app.models import DispatchIntent
from app.repositories.jobs import create_pending_job
from app.schemas import ZapSubmission


@dataclass(frozen=True)
class AcceptedScan:
    job_id: UUID
    scan_type: str
    status: Literal['pending'] = 'pending'


def accept_scan(
    sessions: sessionmaker[Session],
    *,
    submission: ZapSubmission,
    context: TeamContext,
) -> AcceptedScan:
    """Return only after committing both records.

    Submission must already be validated and context must come from
    verified authorization. Persistence errors propagate to the caller.
    """
    with sessions.begin() as session:
        job = create_pending_job(
            session,
            submission=submission,
            context=context,
        )
        session.add(DispatchIntent(job_id=job.job_id))
        session.flush()

        accepted = AcceptedScan(
            job_id=job.job_id,
            scan_type=job.scan_type,
        )

    return accepted
