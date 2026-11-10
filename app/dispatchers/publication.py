"""Publish one committed dispatch intent; no polling or retry loop."""
import logging
from datetime import timedelta
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update

from app.models import DispatchIntent, Job
from app.schemas.job_message import JobMessage


logger = logging.getLogger(__name__)


def publish_job(
    sessions, *, job_id, sns, topic_arn, first_attempt_only=False,
):
    """Return an outcome only after its database transaction commits.

    The caller supplies a configured SNS client with bounded timeouts.
    Exceptions from database operations propagate. A recorded attempt may
    have stopped before contacting SNS; it is not a delivery count.
    """
    if not topic_arn or not topic_arn.strip():
        raise ValueError('A topic ARN is required')

    token = uuid4()
    message = None
    with sessions.begin() as session:
        attempt_guard = (
            DispatchIntent.attempt_count == 0
            if first_attempt_only
            else True
        )
        row = session.execute(
            select(DispatchIntent, Job)
            .join(Job, Job.job_id == DispatchIntent.job_id)
            .where(
                attempt_guard,
                DispatchIntent.job_id == job_id,
                DispatchIntent.published_at.is_(None),
                DispatchIntent.suppressed_at.is_(None),
                DispatchIntent.next_attempt_at <= func.clock_timestamp(),
                or_(
                    DispatchIntent.claim_token.is_(None),
                    DispatchIntent.lease_expires_at <= func.clock_timestamp(),
                ),
                Job.status == 'pending',
                Job.cancellation_requested_at.is_(None),
            )
            .with_for_update(of=DispatchIntent, skip_locked=True)
        ).first()
        if row is None:
            return 'skipped'

        intent, job = row
        intent.attempt_count += 1
        try:
            message = JobMessage.model_validate({
                'schema_version': intent.schema_version,
                'job_id': job.job_id,
                'scan_type': job.scan_type,
                'parameters': job.parameters,
            })
        except ValidationError:
            intent.last_error_code = 'invalid_job_message'
            intent.claim_token = None
            intent.lease_expires_at = None
        else:
            intent.claim_token = token
            intent.lease_expires_at = session.scalar(
                select(func.clock_timestamp())
            ) + timedelta(seconds=60)
            intent.last_error_code = None

    if message is None:
        logger.warning('dispatch_invalid', extra={
            'job_id': str(job_id), 'dispatch_outcome': 'invalid',
        })
        return 'invalid'

    error_code = None
    try:
        response = sns.publish(
            TopicArn=topic_arn,
            Message=message.model_dump_json(),
            MessageAttributes={
                'scan_type': {
                    'DataType': 'String',
                    'StringValue': message.scan_type,
                },
            },
        )
        if not response.get('MessageId'):
            error_code = 'sns_acknowledgement_missing'
    except (BotoCoreError, ClientError):
        error_code = 'sns_publication_failed'

    values = {
        'claim_token': None,
        'lease_expires_at': None,
        'last_error_code': error_code,
    }
    if error_code is None:
        values['published_at'] = func.clock_timestamp()

    with sessions.begin() as session:
        changed = session.execute(
            update(DispatchIntent)
            .where(
                DispatchIntent.job_id == job_id,
                DispatchIntent.claim_token == token,
                DispatchIntent.published_at.is_(None),
                DispatchIntent.suppressed_at.is_(None),
            )
            .values(**values)
        ).rowcount

    outcome = (
        'claim_lost' if changed != 1
        else 'failed' if error_code else 'published'
    )
    logger.info('dispatch_outcome', extra={
        'job_id': str(job_id), 'dispatch_outcome': outcome,
    })
    return outcome
