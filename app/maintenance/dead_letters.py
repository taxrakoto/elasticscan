"""Bounded DLQ observation without deletion, redrive or scanner execution."""
import argparse
import json
import logging
import os
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from app.schemas.job_message import JobMessage


logger = logging.getLogger(__name__)


def canonical_uuid(value):
    if not isinstance(value, str):
        return '-'
    try:
        return str(UUID(value))
    except ValueError:
        return '-'


def safe_count(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        if len(value) <= 16:
            return int(value)
    return 0


def describe_dead_letter(delivery):
    """Extract only approved diagnostic fields from an untrusted body."""
    try:
        payload = json.loads(delivery.get('Body', ''))
    except (TypeError, ValueError):
        payload = None

    job_id = canonical_uuid(
        payload.get('job_id') if isinstance(payload, dict) else None,
    )
    valid = False
    if (
        isinstance(payload, dict)
        and type(payload.get('schema_version')) is int
        and payload['schema_version'] == 1
    ):
        try:
            JobMessage.model_validate(payload)
            valid = True
        except ValidationError:
            pass

    return {
        'event': 'dead_letter_observed',
        'job_id': job_id,
        'message_id': canonical_uuid(delivery.get('MessageId')),
        'schema_valid': valid,
        'dlq_receive_count': safe_count(
            delivery.get('Attributes', {}).get('ApproximateReceiveCount'),
        ),
    }


def observe_dead_letters(sqs, *, source_queue_url, dlq_url, limit=10):
    """Inspect one batch only after checking the source/DLQ relationship."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('Observation limit must be between 1 and 10')
    if not source_queue_url or not dlq_url or source_queue_url == dlq_url:
        raise ValueError('Distinct source and DLQ URLs are required')

    source = sqs.get_queue_attributes(
        QueueUrl=source_queue_url,
        AttributeNames=['QueueArn', 'RedrivePolicy'],
    )['Attributes']
    target = sqs.get_queue_attributes(
        QueueUrl=dlq_url,
        AttributeNames=[
            'QueueArn',
            'RedriveAllowPolicy',
            'ApproximateNumberOfMessages',
            'ApproximateNumberOfMessagesNotVisible',
        ],
    )['Attributes']
    redrive = json.loads(source.get('RedrivePolicy', '{}'))
    allowed = json.loads(target.get('RedriveAllowPolicy', '{}'))
    if (
        not isinstance(redrive, dict)
        or not isinstance(allowed, dict)
        or redrive.get('deadLetterTargetArn') != target.get('QueueArn')
        or allowed.get('redrivePermission') != 'byQueue'
        or not isinstance(allowed.get('sourceQueueArns'), list)
        or source.get('QueueArn') not in allowed.get('sourceQueueArns', [])
    ):
        raise ValueError('Source/DLQ configuration does not match')

    logger.info(json.dumps({
        'event': 'dead_letter_depth',
        'visible': safe_count(target.get('ApproximateNumberOfMessages')),
        'inflight': safe_count(
            target.get('ApproximateNumberOfMessagesNotVisible'),
        ),
    }, sort_keys=True))

    deliveries = sqs.receive_message(
        QueueUrl=dlq_url,
        MaxNumberOfMessages=limit,
        WaitTimeSeconds=0,
        VisibilityTimeout=30,
        MessageSystemAttributeNames=['ApproximateReceiveCount'],
    ).get('Messages', [])

    observations = [describe_dead_letter(item) for item in deliveries]
    for observation in observations:
        logger.warning(json.dumps(observation, sort_keys=True))
    return observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    sqs = None
    try:
        source = os.environ.get('SQS_QUEUE_URL', '').strip()
        target = os.environ.get('SQS_DLQ_URL', '').strip()
        if not source or not target:
            raise ValueError('Queue URLs are required')
        sqs = boto3.client(
            'sqs',
            endpoint_url=os.environ.get('SQS_ENDPOINT_URL') or None,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={'total_max_attempts': 1},
            ),
        )
        observe_dead_letters(
            sqs, source_queue_url=source, dlq_url=target, limit=args.limit,
        )
    except (BotoCoreError, ClientError, ValueError):
        logger.error('{"event":"dead_letter_observation_failed"}')
        return 1
    finally:
        if sqs is not None:
            sqs.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
