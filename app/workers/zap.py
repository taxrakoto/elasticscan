"""Validate one raw message and hand a durably started job to an adapter."""
import json
from typing import Literal

from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from app.models import Job
from app.repositories.lifecycle import start_job
from app.scanners.base import ScannerAdapter, ScanRequest
from app.schemas.job_message import JobMessage


WorkerDecision = Literal[
    'invalid_message',
    'missing_job',
    'inconsistent_message',
    'not_started',
    'invoked',
]


def handle_message(
    body: str,
    *,
    sessions: sessionmaker,
    worker_id: str,
    adapter: ScannerAdapter,
) -> WorkerDecision:
    """No queue acknowledgment or terminal outcome persistence is performed."""
    try:
        payload = json.loads(body)
        if (
            not isinstance(payload, dict)
            or type(payload.get('schema_version')) is not int
            or payload['schema_version'] != 1
        ):
            return 'invalid_message'
        message = JobMessage.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        return 'invalid_message'

    with sessions.begin() as session:
        job = session.get(Job, message.job_id)
        if job is None:
            return 'missing_job'
        if (
            job.scan_type != message.scan_type
            or job.parameters != message.parameters.model_dump(mode='json')
        ):
            return 'inconsistent_message'

        request = ScanRequest(
            job_id=job.job_id,
            url=str(message.parameters.url),
        )
        if not start_job(
            session,
            job_id=job.job_id,
            worker_id=worker_id,
        ):
            return 'not_started'

    # Transaction exit must succeed before external scanner work begins.
    adapter.run(request)
    return 'invoked'
