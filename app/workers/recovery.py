"""Recover abandoned claims independently of active scanner execution."""
import logging

from sqlalchemy.exc import SQLAlchemyError

from app.repositories.recovery import fail_expired_claims


logger = logging.getLogger(__name__)


def recover_once(sessions, *, settings):
    """Commit one bounded batch before reporting successful recovery."""
    with sessions.begin() as session:
        recovered = fail_expired_claims(
            session, limit=settings.recovery_batch_size,
        )
    for job_id in recovered:
        logger.warning(
            'worker_recovery_failed job_id=%s policy=fail_without_rescan',
            job_id,
        )
    return recovered


def recovery_loop(sessions, *, settings, stopped):
    """Retry recovery checks until shutdown; never invoke a scanner."""
    while not stopped.is_set():
        try:
            recover_once(sessions, settings=settings)
        except SQLAlchemyError:
            logger.error('worker_recovery_storage_unavailable')
        if stopped.wait(settings.recovery_interval_seconds):
            return
