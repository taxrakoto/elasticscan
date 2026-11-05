"""Shared request examples and the intended public submission-schema interface."""
import json
from pathlib import Path

from pydantic import BaseModel

import app.schemas


def examples():
    path = Path(__file__).parent / 'fixtures/contracts/v1/scan-submission.json'
    return json.loads(path.read_text())


def submission_model(test):
    # Assert inside a test so an absent feature produces a clear assertion failure,
    # rather than preventing test discovery with an import error.
    model = getattr(app.schemas, 'ScanSubmission', None)
    test.assertIsNotNone(
        model,
        'US-003.1: app.schemas must expose the Pydantic ScanSubmission request model.',
    )
    test.assertTrue(isinstance(model, type) and issubclass(model, BaseModel))
    return model
