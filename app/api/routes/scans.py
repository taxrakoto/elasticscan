"""Authenticated, durable scan submission."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_session_factory
from app.core.target_policy import require_allowed_submission
from app.core.team import TeamContext, require_team_context
from app.schemas import ZapSubmission
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
