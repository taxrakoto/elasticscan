"""Process one delivery with renewable authority and durable acknowledgment."""
import logging
import time
from threading import Event, Thread

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError

from app.models import Job
from app.repositories.lifecycle import complete_job, fail_job, renew_job
from app.scanners.base import ScanInterrupted
from app.scanners.zap_outcomes import execute_zap_scan
from app.schemas.retrieval import JobStatusResponse
from app.workers.zap import handle_message, parse_message


logger = logging.getLogger(__name__)


def terminal_matches(sessions, message):
    with sessions() as session:
        job = session.get(Job, message.job_id)
        if (
            job is None
            or job.scan_type != message.scan_type
            or job.parameters != message.parameters.model_dump(mode='json')
            or job.status not in {'completed', 'failed', 'cancelled'}
        ):
            return False
        JobStatusResponse.model_validate(job)
        return True


class Execution:
    def __init__(
        self, *, sessions, sqs, queue_url, receipt_handle,
        worker_id, settings, stopped, adapter_factory,
    ):
        self.sessions = sessions
        self.sqs = sqs
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.worker_id = worker_id
        self.settings = settings
        self.stopped = stopped
        self.adapter_factory = adapter_factory
        self.cancel = Event()
        self.done = Event()

    def pulse(self):
        if self.stopped.is_set():
            self.cancel.set()
            return False
        try:
            self.sqs.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=self.receipt_handle,
                VisibilityTimeout=self.settings.visibility_seconds,
            )
            with self.sessions.begin() as session:
                job = session.get(Job, self.request.job_id)
                if (
                    job is None
                    or job.cancellation_requested_at is not None
                ):
                    renewed = False
                else:
                    renewed = renew_job(
                        session,
                        job_id=self.request.job_id,
                        worker_id=self.worker_id,
                        claim_token=self.request.claim_token,
                        lease_seconds=self.settings.lease_seconds,
                    )
            if not renewed:
                self.cancel.set()
                return False
        except (SQLAlchemyError, BotoCoreError, ClientError):
            logger.warning('worker_renewal_uncertain')
            self.cancel.set()
            return False
        return True

    def watch(self):
        next_pulse = time.monotonic() + self.settings.heartbeat_seconds
        while not self.done.wait(0.2):
            if self.stopped.is_set():
                self.cancel.set()
                return
            if time.monotonic() >= next_pulse:
                if not self.pulse():
                    return
                next_pulse = (
                    time.monotonic() + self.settings.heartbeat_seconds
                )

    def run(self, request):
        self.request = request
        if not self.pulse():
            raise ScanInterrupted('Execution authority unavailable')

        watcher = Thread(target=self.watch, daemon=True)
        watcher.start()
        try:
            outcome = execute_zap_scan(
                self.adapter_factory(self.cancel), request,
            )
        finally:
            self.done.set()
            watcher.join()

        if self.cancel.is_set() or self.stopped.is_set():
            raise ScanInterrupted('Execution authority interrupted')

        with self.sessions.begin() as session:
            common = {
                'job_id': request.job_id,
                'worker_id': self.worker_id,
                'claim_token': request.claim_token,
            }
            if outcome.status == 'completed':
                saved = complete_job(
                    session, counts=outcome.counts, **common,
                )
            else:
                saved = fail_job(session, outcome=outcome, **common)
            if not saved:
                raise ScanInterrupted('Terminal write rejected')
        return None


def consume_one(
    delivery, *, sessions, sqs, queue_url, worker_id,
    settings, stopped, adapter_factory, policy=None,
):
    if stopped.is_set():
        return 'shutdown'

    message = parse_message(delivery['Body'])
    if message is None:
        return 'invalid_message'

    def acknowledge():
        sqs.delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=delivery['ReceiptHandle'],
        )

    if terminal_matches(sessions, message):
        acknowledge()
        return 'terminal_confirmed'

    execution = Execution(
        sessions=sessions,
        sqs=sqs,
        queue_url=queue_url,
        receipt_handle=delivery['ReceiptHandle'],
        worker_id=worker_id,
        settings=settings,
        stopped=stopped,
        adapter_factory=adapter_factory,
    )
    try:
        decision = handle_message(
            delivery['Body'],
            sessions=sessions,
            worker_id=worker_id,
            adapter=execution,
            lease_seconds=settings.lease_seconds,
            policy=policy,
            max_attempts=1,
        )
    except ScanInterrupted:
        return 'interrupted'

    if terminal_matches(sessions, message):
        acknowledge()
        return 'processed'
    return decision
