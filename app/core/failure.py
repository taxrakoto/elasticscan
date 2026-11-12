"""Fixed safe errors for durable scan failures."""


_MESSAGES = {
    'scanner_timeout':
        'Scanner execution exceeded its time limit.',
    'scanner_execution_failed':
        'Scanner execution failed.',
    'scanner_output_invalid':
        'Scanner output was missing or invalid.',
    'scanner_execution_invalid':
        'Scanner execution did not satisfy the baseline requirements.',
    'dispatch_exhausted':
        'Scan dispatch attempts were exhausted.',
    'recovery_exhausted':
        'Scan recovery attempts were exhausted.',
}

SCANNER_FAILURE_CODES = frozenset({
    'scanner_timeout',
    'scanner_execution_failed',
    'scanner_output_invalid',
    'scanner_execution_invalid',
})

EXHAUSTED_FAILURE_CODES = frozenset({
    'dispatch_exhausted',
    'recovery_exhausted',
})


def safe_error(code: str) -> dict[str, str]:
    """Return only an approved code and its canonical safe message."""
    if not isinstance(code, str) or code not in _MESSAGES:
        raise ValueError('Unknown failure code')
    return {'code': code, 'message': _MESSAGES[code]}
