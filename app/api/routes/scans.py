"""Authenticated scan submission and team-scoped retrieval."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory
from app.core.target_policy import require_allowed_submission
from app.core.team import TeamContext, require_team_context
from app.repositories.jobs import find_team_job
from app.schemas import ZapSubmission
from app.schemas.retrieval import JobStatusResponse
from app.schemas.submission import ScanAcceptance
from app.services.acceptance import accept_scan


router = APIRouter(tags=['scans'])


@router.post(
    '/scan',
    status_code=202,
    response_model=ScanAcceptance,
    responses={
        401: {'description': 'Missing or invalid bearer authentication'},
        403: {'description': 'Unauthorized team context or target'},
        503: {'description': 'Durable acceptance could not be confirmed'},
    },
)
def submit_scan(
    submission: Annotated[
        ZapSubmission, Depends(require_allowed_submission),
    ],
    context: Annotated[TeamContext, Depends(require_team_context)],
    sessions: Annotated[
        sessionmaker[Session], Depends(get_session_factory),
    ],
) -> ScanAcceptance:
    try:
        accepted = accept_scan(
            sessions,
            submission=submission,
            context=context,
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail='Durable acceptance could not be confirmed',
        ) from None

    return ScanAcceptance.model_validate(accepted)


@router.get(
    '/scan/{job_id}',
    response_model=JobStatusResponse,
    responses={
        401: {'description': 'Missing or invalid bearer authentication'},
        403: {'description': 'Missing or unauthorized team context'},
        404: {'description': 'Job not found'},
        503: {'description': 'Job retrieval is temporarily unavailable'},
    },
)
def retrieve_scan(
    job_id: UUID,
    context: Annotated[TeamContext, Depends(require_team_context)],
    sessions: Annotated[
        sessionmaker[Session], Depends(get_session_factory),
    ],
) -> JobStatusResponse:
    try:
        with sessions() as session:
            job = find_team_job(
                session,
                job_id=job_id,
                context=context,
            )
            if job is None:
                raise HTTPException(
                    status_code=404,
                    detail='Job not found',
                )
            return JobStatusResponse.model_validate(job)
    except SQLAlchemyError:
        raise HTTPException(
            status_code=503,
            detail='Job retrieval is temporarily unavailable',
        ) from None
