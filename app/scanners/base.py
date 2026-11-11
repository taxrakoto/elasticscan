"""Scanner handoff contract; concrete ZAP execution is separate."""
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID


@dataclass(frozen=True)
class ScanRequest:
    job_id: UUID
    url: str
    claim_token: UUID
    scope_regex: str


@dataclass(frozen=True)
class ScannerOutput:
    return_code: int
    report: str | None
    passive_complete: bool
    active_scan_count: int | None
    visited_urls: tuple[str, ...]


@dataclass(frozen=True)
class SeverityCounts:
    high: int = 0
    medium: int = 0
    low: int = 0


@dataclass(frozen=True)
class ScanOutcome:
    counts: SeverityCounts | None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def status(self) -> Literal['completed', 'failed']:
        return 'completed' if self.counts is not None else 'failed'


class ScannerAdapter(Protocol):
    def run(self, request: ScanRequest) -> ScannerOutput | None:
        """Execute the request or raise; return is not durable completion."""
        ...
