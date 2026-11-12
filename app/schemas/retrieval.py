"""Public lifecycle-specific job retrieval."""
from datetime import timezone
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictInt,
    field_validator, model_validator,
)

from app.core.failure import safe_error


class SeveritySummary(BaseModel):
    model_config = ConfigDict(extra='forbid')

    high: StrictInt = Field(ge=0)
    medium: StrictInt = Field(ge=0)
    low: StrictInt = Field(ge=0)


class SafeFailure(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str
    message: str

    @model_validator(mode='before')
    @classmethod
    def canonical_error(cls, value):
        if not isinstance(value, dict):
            raise ValueError('An error object is required')
        return safe_error(value.get('code'))


class JobStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra='forbid')

    job_id: UUID
    scan_type: str
    status: Literal[
        'pending', 'running', 'completed', 'failed', 'cancelled',
    ]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    result: SeveritySummary | None
    error: SafeFailure | None
    cancellation_requested_by: str | None
    cancellation_requested_at: AwareDatetime | None
    cancelled_at: AwareDatetime | None

    @field_validator(
        'created_at', 'updated_at', 'started_at', 'finished_at',
        'cancellation_requested_at', 'cancelled_at',
    )
    @classmethod
    def utc_timestamp(cls, value):
        return None if value is None else value.astimezone(timezone.utc)

    @model_validator(mode='after')
    def check_lifecycle(self):
        terminal = self.status in {'completed', 'failed', 'cancelled'}
        if terminal != (self.finished_at is not None):
            raise ValueError('Finish time must match terminal state')
        if self.status == 'pending' and self.started_at is not None:
            raise ValueError('Pending jobs cannot have a start time')
        if (
            self.status in {'running', 'completed'}
            and self.started_at is None
        ):
            raise ValueError('A start time is required')
        if (self.result is not None) != (self.status == 'completed'):
            raise ValueError('Results belong only to completed jobs')
        if (self.error is not None) != (self.status == 'failed'):
            raise ValueError('Errors belong only to failed jobs')

        requester = self.cancellation_requested_by
        requested_at = self.cancellation_requested_at
        if (requester is None) != (requested_at is None):
            raise ValueError('Cancellation request metadata must be paired')
        if requester is not None and not requester.strip():
            raise ValueError('Cancellation requester cannot be empty')
        if self.status == 'cancelled':
            if requested_at is None or self.cancelled_at is None:
                raise ValueError('Cancelled jobs require cancellation metadata')
            if self.cancelled_at != self.finished_at:
                raise ValueError('Cancellation and finish times must agree')
        elif self.cancelled_at is not None:
            raise ValueError('Only cancelled jobs have a cancellation time')
        return self
