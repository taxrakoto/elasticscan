"""Validated timing configuration for one sequential worker process."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    visibility_seconds: int = 90
    recovery_interval_seconds: int = 5
    recovery_batch_size: int = 10
    retry_max_attempts: int = 3
    retry_base_delay_seconds: int = 2

    def __post_init__(self):
        if (
            type(self.retry_max_attempts) is not int
            or not 1 <= self.retry_max_attempts <= 10
        ):
            raise ValueError('Retry attempts must be between 1 and 10')
        if (
            type(self.retry_base_delay_seconds) is not int
            or not 1 <= self.retry_base_delay_seconds <= 30
        ):
            raise ValueError('Retry base delay must be between 1 and 30 seconds')
        if (
            type(self.recovery_interval_seconds) is not int
            or not 1 <= self.recovery_interval_seconds <= 300
        ):
            raise ValueError('Recovery interval must be between 1 and 300 seconds')
        if (
            type(self.recovery_batch_size) is not int
            or not 1 <= self.recovery_batch_size <= 100
        ):
            raise ValueError('Recovery batch size must be between 1 and 100')
        values = (
            self.lease_seconds,
            self.heartbeat_seconds,
            self.visibility_seconds,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError('Worker timing values must be positive integers')
        if self.lease_seconds > 3600:
            raise ValueError('Execution lease cannot exceed 3600 seconds')
        if self.heartbeat_seconds * 3 > self.lease_seconds:
            raise ValueError('Heartbeat must be at most one third of the lease')
        if self.visibility_seconds < (
            self.lease_seconds + 2 * self.heartbeat_seconds
        ):
            raise ValueError('Visibility must extend beyond the execution lease')
        if self.visibility_seconds > 43200:
            raise ValueError('Visibility exceeds the SQS maximum')

    @classmethod
    def from_environment(cls):
        return cls(
            lease_seconds=int(os.environ.get(
                'WORKER_LEASE_SECONDS', '60'
            )),
            heartbeat_seconds=int(os.environ.get(
                'WORKER_HEARTBEAT_SECONDS', '15'
            )),
            visibility_seconds=int(os.environ.get(
                'WORKER_VISIBILITY_SECONDS', '90'
            )),
            recovery_interval_seconds=int(os.environ.get(
                'WORKER_RECOVERY_INTERVAL_SECONDS', '5'
            )),
            recovery_batch_size=int(os.environ.get(
                'WORKER_RECOVERY_BATCH_SIZE', '10'
            )),
            retry_max_attempts=int(os.environ.get(
                'WORKER_RETRY_MAX_ATTEMPTS', '3'
            )),
            retry_base_delay_seconds=int(os.environ.get(
                'WORKER_RETRY_BASE_DELAY_SECONDS', '2'
            )),
        )
