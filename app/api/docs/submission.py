"""Documentation metadata for the existing scan submission endpoint."""

SUBMISSION_SUMMARY = 'Submit an authorized ZAP scan'

SUBMISSION_DESCRIPTION = """
Submit JSON containing `scan_type: "zap"` and `parameters.url`.
Unknown fields, including `team_id` and `created_by`, are rejected.

### Authentication and team selection

Authenticate with `Authorization: Bearer <access-token>` using an API access
token from the configured trusted issuer.

`X-Team-ID` selects one exact team ID from verified token membership; it grants
no membership. It may be omitted for one distinct team, but is required for
multiple teams. Missing, malformed, ambiguous, unauthorized, or repeated team
selection is denied with HTTP 403.

### Target authorization

The URL must pass the configured origin allowlist and public-address checks.
The schema example uses an isolated test fixture, not a live target.

### Durable acceptance

HTTP 202 is returned after the job and dispatch intent commit. Its `pending`
status is an acceptance snapshot, not current worker progress or proof of scan
completion. Dispatch and scan execution remain pending implementation.

### Validation errors

HTTP 422 reports invalid request fields in `detail[]`, with:

- `loc`: the field location, such as `["body", "parameters", "url"]`.
- `msg`: a human-readable explanation.
- `type`: the validation error category.

Missing URL, unsupported scan type, malformed URL, and extra fields are
validation errors. Authentication or authorization may reject a request before
body validation.

### Uncertain acceptance

HTTP 503 means durable acceptance could not be confirmed. A connection failure
during commit can leave the outcome uncertain. Automatic retry deduplication
is not implemented.
"""

SUBMISSION_RESPONSES = {
    202: {
        'description': 'Job and dispatch intent durably accepted',
    },
    401: {
        'description': 'Missing or invalid bearer authentication',
        'headers': {
            'WWW-Authenticate': {
                'description': 'Bearer authentication challenge',
                'schema': {
                    'type': 'string',
                    'example': 'Bearer',
                },
            },
        },
        'content': {
            'application/json': {
                'example': {
                    'detail': 'Invalid or missing access token',
                },
            },
        },
    },
    403: {
        'description': 'Unauthorized team context or target',
        'content': {
            'application/json': {
                'examples': {
                    'team': {
                        'value': {
                            'detail': 'Missing or unauthorized team context',
                        },
                    },
                    'target': {
                        'value': {
                            'detail': 'Target is not allowed',
                        },
                    },
                },
            },
        },
    },
    503: {
        'description': 'Durable acceptance could not be confirmed',
        'content': {
            'application/json': {
                'example': {
                    'detail': 'Durable acceptance could not be confirmed',
                },
            },
        },
    },
}
