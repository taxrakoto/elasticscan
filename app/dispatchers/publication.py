"""Publish committed intent with bounded, durable retry scheduling."""
import logging
from datetime import timedelta
from random import uniform
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update

from app.models import DispatchIntent, Job
from app.schemas.job_message import JobMessage


logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


def retry_delay(attempt_count):
    """Return jittered exponential backoff for a positive attempt count."""
    ceiling = min(2 ** min(attempt_count, 5), 30)
    return timedelta(seconds=uniform(ceiling / 2, ceiling))


def dispatch_conditions():
    """Share eligibility rules between discovery and the atomic claim."""
    return (
        DispatchIntent.attempt_count < MAX_ATTEMPTS,
        or_(
            DispatchIntent.last_error_code.is_(None),
            DispatchIntent.last_error_code != 'invalid_job_message',
        ),
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


def publish_job(
    sessions, *, job_id, sns, topic_arn, first_attempt_only=False,
):
    """Return a publication outcome only after its transaction commits.

    Database errors propagate. Attempts count committed claims, including
    interrupted attempts, rather than confirmed network sends.
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
                *dispatch_conditions(),
                attempt_guard,
                DispatchIntent.job_id == job_id,
            )
            .with_for_update(of=DispatchIntent, skip_locked=True)
        ).first()
        if row is None:
            return 'skipped'

        intent, job = row
        intent.attempt_count += 1
        attempt_count = intent.attempt_count
        delay = retry_delay(attempt_count)
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
            now = session.scalar(select(func.clock_timestamp()))
            intent.claim_token = token
            intent.lease_expires_at = now + timedelta(
                seconds=LEASE_SECONDS,
            )
            # Persist crash recovery timing before contacting SNS.
            intent.next_attempt_at = intent.lease_expires_at + delay
            intent.last_error_code = None

    if message is None:
        logger.warning('dispatch_invalid', extra={
            'job_id': str(job_id),
            'dispatch_outcome': 'invalid',
            'attempt_count': attempt_count,
            'last_error_code': 'invalid_job_message',
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
    retry_at = None
    with sessions.begin() as session:
        if error_code is None:
            values['published_at'] = func.clock_timestamp()
        elif attempt_count < MAX_ATTEMPTS:
            retry_at = session.scalar(
                select(func.clock_timestamp())
            ) + delay
            values['next_attempt_at'] = retry_at

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

    if changed != 1:
        outcome = 'claim_lost'
    elif error_code is None:
        outcome = 'published'
    elif attempt_count >= MAX_ATTEMPTS:
        outcome = 'exhausted'
    else:
        outcome = 'failed'

    logger.info('dispatch_outcome', extra={
        'job_id': str(job_id),
        'dispatch_outcome': outcome,
        'attempt_count': attempt_count,
        'next_attempt_at': (
            retry_at.isoformat()
            if outcome == 'failed' else '-'
        ),
        'last_error_code': error_code or '-',
    })
    return outcome
