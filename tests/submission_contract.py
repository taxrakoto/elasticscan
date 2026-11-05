"""Shared submission request examples used by unit and integration tests."""
import json
from pathlib import Path


def examples():
    path = Path(__file__).parent / 'fixtures/contracts/v1/scan-submission.json'
    return json.loads(path.read_text())
