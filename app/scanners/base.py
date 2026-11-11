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


class ScannerAdapter(Protocol):
    def run(self, request: ScanRequest) -> None:
        """Execute the request or raise; return is not durable completion."""
        ...
