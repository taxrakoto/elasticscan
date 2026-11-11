"""Trusted hooks for the pinned baseline script, not user-provided hooks."""
from functools import partial
import json
import os
from pathlib import Path
import re
from uuid import UUID

from app.scanners.base import ScanRequest
from app.scanners.scope import configure_scope


def work_directory() -> Path:
    return Path(os.environ['ELASTICSCAN_BASELINE_DIR'])


def saved_request() -> ScanRequest:
    payload = json.loads(
        (work_directory() / 'request.json').read_text()
    )
    return ScanRequest(
        job_id=UUID(payload['job_id']),
        url=payload['url'],
        claim_token=UUID(payload['claim_token']),
        scope_regex=payload['scope_regex'],
    )


def start_zap(port, extra_zap_params):
    """Replace packaged wildcard defaults with the fixed local-only options."""
    import zap_common

    def local_options(mode, listen_port, extra_params):
        return [
            '/zap/zap-x.sh', mode,
            '-port', str(listen_port),
            '-config', 'database.recoverylog=false',
            '-config', 'api.disablekey=true',
            *extra_params,
        ]

    zap_common.create_start_options = local_options


def zap_started(zap, target):
    import zap_common

    request = saved_request()
    if target != request.url:
        raise RuntimeError('Baseline target does not match saved request')

    zap_common.context_name = configure_scope(zap, request)

    # The baseline's initial access must not automatically follow redirects.
    zap.urlopen = partial(zap.urlopen, allow_redirects=False)


def zap_pre_shutdown(zap):
    request = saved_request()
    pending = int(zap.pscan.records_to_scan)
    active = len(zap.ascan.scans)
    visited = list(zap.core.urls())

    if pending != 0:
        raise RuntimeError('Passive analysis did not finish')
    if active != 0:
        raise RuntimeError('Unexpected active scanner execution')
    if any(
        re.fullmatch(request.scope_regex, url) is None
        for url in visited
    ):
        raise RuntimeError('Out-of-origin scanner activity detected')

    (work_directory() / 'evidence.json').write_text(json.dumps({
        'passive_pending': pending,
        'active_scan_count': active,
        'visited_urls': visited,
    }))
