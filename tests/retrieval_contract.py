"""Version-one retrieval examples, including proposed cancellation."""
from datetime import datetime, timedelta, timezone


CONTRACT_VERSION = 1

PUBLIC_FIELDS = {
    'job_id', 'scan_type', 'status',
    'created_at', 'updated_at', 'started_at', 'finished_at',
    'result', 'error',
    'cancellation_requested_by', 'cancellation_requested_at',
    'cancelled_at',
}

TIMESTAMP_FIELDS = (
    'created_at', 'updated_at', 'started_at', 'finished_at',
    'cancellation_requested_at', 'cancelled_at',
)


def retrieval_example(status):
    now = datetime(2026, 11, 12, 12, tzinfo=timezone(timedelta(hours=3)))
    data = {
        'job_id': '550e8400-e29b-41d4-a716-446655440000',
        'scan_type': 'zap',
        'status': status,
        'created_at': now,
        'updated_at': now,
        'started_at': None,
        'finished_at': None,
        'result': None,
        'error': None,
        'cancellation_requested_by': None,
        'cancellation_requested_at': None,
        'cancelled_at': None,
    }
    if status in {'running', 'completed', 'failed'}:
        data['started_at'] = now
    if status in {'completed', 'failed', 'cancelled'}:
        data['finished_at'] = now
    if status == 'completed':
        data['result'] = {'high': 1, 'medium': 2, 'low': 3}
    if status == 'failed':
        data['error'] = {
            'code': 'scanner_timeout',
            'message': 'Scanner execution exceeded its time limit.',
        }
    if status == 'cancelled':
        data.update(
            cancellation_requested_by='alice',
            cancellation_requested_at=now,
            cancelled_at=now,
        )
    return data
