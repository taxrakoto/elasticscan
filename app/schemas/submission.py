"""Typed scan submission requests, independent of persistence and execution."""
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


SUBMISSION_EXAMPLE = {
    'scan_type': 'zap',
    'parameters': {
        'url': 'https://approved.example.invalid/',
    },
}

ACCEPTANCE_EXAMPLE = {
    'job_id': '123e4567-e89b-42d3-a456-426614174000',
    'scan_type': 'zap',
    'status': 'pending',
}

JOB_ID_DESCRIPTION = 'Identifier of the durably accepted job.'
SCAN_TYPE_DESCRIPTION = 'Only zap is supported by the MVP.'
ACCEPTANCE_STATUS_DESCRIPTION = (
    'Acceptance snapshot; workers may subsequently change status.'
)
TARGET_URL_DESCRIPTION = (
    'Required HTTP or HTTPS URL. Syntax validation is followed by '
    'the configured origin allowlist and public-address checks.'
)
PARAMETERS_DESCRIPTION = (
    'Scanner input; ownership fields and unknown fields are rejected.'
)


class ScanAcceptance(BaseModel):
    """Snapshot of durable acceptance, not current worker progress."""

    model_config = ConfigDict(
        from_attributes=True,
        extra='forbid',
        json_schema_extra={'examples': [ACCEPTANCE_EXAMPLE]},
    )

    job_id: UUID = Field(description=JOB_ID_DESCRIPTION)
    scan_type: Literal['zap'] = Field(description=SCAN_TYPE_DESCRIPTION)
    status: Literal['pending'] = Field(
        description=ACCEPTANCE_STATUS_DESCRIPTION,
    )


class ZapParameters(BaseModel):
    """ZAP input with HTTP/HTTPS URL syntax validation."""

    model_config = ConfigDict(extra='forbid')

    url: AnyHttpUrl = Field(description=TARGET_URL_DESCRIPTION)


class ZapSubmission(BaseModel):
    """Public request body for a ZAP scan submission."""

    model_config = ConfigDict(
        extra='forbid',
        json_schema_extra={'examples': [SUBMISSION_EXAMPLE]},
    )

    scan_type: Literal['zap'] = Field(description=SCAN_TYPE_DESCRIPTION)
    parameters: ZapParameters = Field(description=PARAMETERS_DESCRIPTION)
