"""Validate one raw message and hand a durably started job to an adapter."""
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import sessionmaker

from app.core.target_policy import TargetPolicy, get_target_policy
from app.models import Job
from app.repositories.lifecycle import claim_job
from app.scanners.base import ScannerAdapter, ScanRequest
from app.scanners.scope import fixed_origin_scope
from app.schemas.job_message import JobMessage


WorkerDecision = Literal[
    'invalid_message',
    'missing_job',
    'inconsistent_message',
    'target_denied',
    'not_started',
    'invoked',
]


def handle_message(
    body: str,
    *,
    sessions: sessionmaker,
    worker_id: str,
    adapter: ScannerAdapter,
    lease_seconds: int = 60,
    policy: TargetPolicy | None = None,
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

        execution_policy = policy if policy is not None else get_target_policy()
        if not execution_policy.allowed_origins:
            return 'target_denied'
        try:
            scope_regex = fixed_origin_scope(
                message.parameters.url, execution_policy,
            )
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            return 'target_denied'

        token = claim_job(
            session,
            job_id=job.job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if token is None:
            return 'not_started'
        request = ScanRequest(
            job_id=job.job_id,
            url=str(message.parameters.url),
            claim_token=token,
            scope_regex=scope_regex,
        )

    # Transaction exit must succeed before external scanner work begins.
    adapter.run(request)
    return 'invoked'
