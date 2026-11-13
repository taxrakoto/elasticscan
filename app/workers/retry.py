"""Bounded retries for explicitly selected repeatable worker operations."""
import logging

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from sqlalchemy.exc import OperationalError


logger = logging.getLogger(__name__)

NETWORK_ERRORS = (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
TRANSIENT_AWS_CODES = frozenset({
    'InternalError',
    'InternalFailure',
    'RequestTimeout',
    'RequestTimeoutException',
    'ServiceUnavailable',
    'Throttling',
    'ThrottlingException',
    'RequestThrottled',
})
TRANSIENT_SQLSTATES = frozenset({
    '40001',  # Serialization failure.
    '40P01',  # Deadlock.
    '55P03',  # Lock unavailable.
    '53300',  # Too many connections.
    '57P01',  # Administrative shutdown.
    '57P02',  # Crash shutdown.
    '57P03',  # Cannot connect now.
})


class RetryStopped(Exception):
    """Shutdown prevented another infrastructure operation attempt."""


def retry_category(error):
    """Return an approved transient category, or None for no retry."""
    if isinstance(error, NETWORK_ERRORS):
        return 'queue_connection'

    if isinstance(error, ClientError):
        response = error.response
        code = response.get('Error', {}).get('Code')
        status = response.get('ResponseMetadata', {}).get('HTTPStatusCode')
        if code in TRANSIENT_AWS_CODES or status in (429, 500, 502, 503, 504):
            return 'queue_service'
        return None

    if isinstance(error, OperationalError):
        state = getattr(error.orig, 'sqlstate', None)
        if (
            state is None
            or state.startswith('08')
            or state in TRANSIENT_SQLSTATES
        ):
            return 'database_connection'
    return None


def retry_operation(operation, *, name, settings, stopped, job_id=None):
    """Return an operation's result or raise its final original exception."""
    for attempt in range(1, settings.retry_max_attempts + 1):
        if stopped.is_set():
            raise RetryStopped('Worker is stopping')
        try:
            return operation()
        except Exception as error:
            category = retry_category(error)
            if category is None:
                logger.error(
                    'worker_operation_not_retryable operation=%s job_id=%s',
                    name, job_id or '-',
                )
                raise

            if attempt == settings.retry_max_attempts:
                logger.error(
                    'worker_retry_exhausted operation=%s job_id=%s '
                    'category=%s attempts=%s',
                    name, job_id or '-', category, attempt,
                )
                raise

            delay = min(
                settings.retry_base_delay_seconds * 2 ** (attempt - 1),
                60,
            )
            logger.warning(
                'worker_retry operation=%s job_id=%s category=%s '
                'attempt=%s next_delay_seconds=%s',
                name, job_id or '-', category, attempt, delay,
            )
            if stopped.wait(delay):
                raise RetryStopped('Worker stopped during retry delay') from None
