"""Validated timing configuration for one sequential worker process."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    visibility_seconds: int = 90

    def __post_init__(self):
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
        )
