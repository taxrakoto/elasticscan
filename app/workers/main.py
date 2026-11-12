"""Run one sequential ZAP worker with conservative abandoned-job recovery."""
import logging
import os
import signal
import socket
from pathlib import Path
from threading import Event
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.target_policy import get_target_policy
from app.db.session import database_url, make_session_factory
from app.repositories.recovery import fail_expired_claims
from app.scanners.baseline import ZapBaselineAdapter
from app.workers.consumer import consume_one
from app.workers.settings import WorkerSettings


logger = logging.getLogger(__name__)
READY_FILE = Path('/tmp/worker-ready')


def run(
    sessions, *, sqs, queue_url, worker_id,
    settings, stopped, adapter_factory, policy=None,
):
    while not stopped.is_set():
        try:
            with sessions.begin() as session:
                recovered = fail_expired_claims(session)
            for job_id in recovered:
                logger.warning('worker_recovery_failed job_id=%s', job_id)

            if stopped.is_set():
                break
            deliveries = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=10,
                VisibilityTimeout=settings.visibility_seconds,
            ).get('Messages', [])

            for delivery in deliveries:
                if stopped.is_set():
                    break
                decision = consume_one(
                    delivery,
                    sessions=sessions,
                    sqs=sqs,
                    queue_url=queue_url,
                    worker_id=worker_id,
                    settings=settings,
                    stopped=stopped,
                    adapter_factory=adapter_factory,
                    policy=policy,
                )
                logger.info('worker_delivery outcome=%s', decision)
        except (
            SQLAlchemyError, BotoCoreError, ClientError, ValidationError,
        ):
            logger.error('worker_dependency_or_storage_unavailable')
            stopped.wait(2)


def main():
    logging.basicConfig(
        level=logging.INFO, format='%(levelname)s %(message)s',
    )
    settings = WorkerSettings.from_environment()
    policy = get_target_policy()
    queue_url = os.environ.get('SQS_QUEUE_URL', '').strip()
    if not queue_url:
        raise ValueError('SQS_QUEUE_URL is required')

    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    engine = create_engine(
        database_url(),
        pool_pre_ping=True,
        connect_args={
            'connect_timeout': 3,
            'options': (
                '-c timezone=UTC '
                '-c statement_timeout=5000 '
                '-c lock_timeout=2000'
            ),
            'keepalives_idle': 5,
            'keepalives_interval': 1,
            'keepalives_count': 3,
            'tcp_user_timeout': 5000,
        },
    )
    sqs = boto3.client(
        'sqs',
        endpoint_url=os.environ.get('SQS_ENDPOINT_URL') or None,
        config=Config(
            connect_timeout=3,
            read_timeout=15,
            retries={'total_max_attempts': 1},
        ),
    )
    try:
        READY_FILE.touch()
        run(
            make_session_factory(engine),
            sqs=sqs,
            queue_url=queue_url,
            worker_id=f'{socket.gethostname()}-{uuid4().hex}',
            settings=settings,
            stopped=stopped,
            adapter_factory=lambda cancel: (
                ZapBaselineAdapter.from_environment(stop_event=cancel)
            ),
            policy=policy,
        )
    finally:
        READY_FILE.unlink(missing_ok=True)
        sqs.close()
        engine.dispose()


if __name__ == '__main__':
    main()
