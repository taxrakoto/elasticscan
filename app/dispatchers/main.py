"""Run first-attempt publication independently of the HTTP API."""
import logging
import os
import signal
from pathlib import Path
from threading import Event

import boto3
from botocore.config import Config
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import make_engine, make_session_factory
from app.dispatchers.publication import publish_job
from app.models import DispatchIntent, Job


logger = logging.getLogger(__name__)
READY_FILE = Path('/tmp/dispatcher-ready')


def run_pass(sessions, *, sns, topic_arn, stopped):
    """Discover at most ten first attempts, then publish outside the read."""
    if stopped.is_set():
        return []
    with sessions() as session:
        job_ids = list(session.scalars(
            select(DispatchIntent.job_id)
            .join(Job, Job.job_id == DispatchIntent.job_id)
            .where(
                DispatchIntent.attempt_count == 0,
                DispatchIntent.published_at.is_(None),
                DispatchIntent.suppressed_at.is_(None),
                DispatchIntent.claim_token.is_(None),
                DispatchIntent.next_attempt_at <= func.clock_timestamp(),
                Job.status == 'pending',
                Job.cancellation_requested_at.is_(None),
            )
            .order_by(DispatchIntent.created_at, DispatchIntent.id)
            .limit(10)
        ))

    outcomes = []
    for job_id in job_ids:
        if stopped.is_set():
            break
        outcomes.append(publish_job(
            sessions,
            job_id=job_id,
            sns=sns,
            topic_arn=topic_arn,
            first_attempt_only=True,
        ))
    return outcomes


def run(sessions, *, sns, topic_arn, stopped):
    while not stopped.is_set():
        try:
            run_pass(
                sessions, sns=sns, topic_arn=topic_arn, stopped=stopped,
            )
        except SQLAlchemyError:
            logger.error('dispatcher_database_unavailable')
        stopped.wait(2)


def main():
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '%(levelname)s %(message)s '
        'job_id=%(job_id)s outcome=%(dispatch_outcome)s',
        defaults={'job_id': '-', 'dispatch_outcome': '-'},
    ))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    topic_arn = os.environ.get('SNS_TOPIC_ARN', '').strip()
    if not topic_arn:
        raise ValueError('SNS_TOPIC_ARN is required')

    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    engine = make_engine()
    try:
        sns = boto3.client(
            'sns',
            endpoint_url=os.environ.get('SNS_ENDPOINT_URL') or None,
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={'total_max_attempts': 1},
            ),
        )
        try:
            READY_FILE.touch()
            logger.info('dispatcher_started')
            run(
                make_session_factory(engine), sns=sns,
                topic_arn=topic_arn, stopped=stopped,
            )
        finally:
            READY_FILE.unlink(missing_ok=True)
            sns.close()
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
