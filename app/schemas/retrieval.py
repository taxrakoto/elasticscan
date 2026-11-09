"""Initial public job-retrieval response."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JobStatusResponse(BaseModel):
    """Current persisted status; lifecycle details follow in US-009.2."""

    model_config = ConfigDict(from_attributes=True, extra='forbid')

    job_id: UUID
    scan_type: str
    status: Literal[
        'pending', 'running', 'completed', 'failed', 'cancelled',
    ]
