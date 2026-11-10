"""Version-one messages shared by publishers and future workers."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.submission import ZapParameters


class JobMessage(BaseModel):
    model_config = ConfigDict(extra='forbid')

    schema_version: Literal[1]
    job_id: UUID
    scan_type: Literal['zap']
    parameters: ZapParameters
