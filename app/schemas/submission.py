"""Typed scan submission requests, independent of persistence and execution."""
from typing import Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict


class ScanAcceptance(BaseModel):
    """Snapshot of durable acceptance, not current worker progress."""

    model_config = ConfigDict(from_attributes=True, extra='forbid')

    job_id: UUID
    scan_type: Literal['zap']
    status: Literal['pending']


class ZapParameters(BaseModel):
    """ZAP input with HTTP/HTTPS URL syntax validation."""

    model_config = ConfigDict(extra='forbid')

    url: AnyHttpUrl


class ZapSubmission(BaseModel):
    """Public request body for a ZAP scan submission."""

    model_config = ConfigDict(extra='forbid')

    scan_type: Literal['zap']
    parameters: ZapParameters
