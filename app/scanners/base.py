"""Scanner handoff contract; concrete ZAP execution is separate."""
from dataclasses import dataclass
from typing import Protocol
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


class ScannerAdapter(Protocol):
    def run(self, request: ScanRequest) -> ScannerOutput | None:
        """Execute the request or raise; return is not durable completion."""
        ...
