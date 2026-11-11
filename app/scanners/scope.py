"""Single-origin validation and reusable ZAP context configuration."""
import re
from ipaddress import ip_address

from pydantic import AnyHttpUrl

from app.core.target_policy import TargetPolicy, origin
from app.scanners.base import ScanRequest


def fixed_origin_scope(
    url: AnyHttpUrl,
    policy: TargetPolicy,
) -> str:
    """Recheck the target and return an exact-origin Java-compatible regex."""
    if len(policy.allowed_origins) != 1:
        raise ValueError('Scanner execution requires one configured origin')
    policy.validate(url)
    scheme, host, port = origin(url)

    try:
        address = ip_address(host)
    except ValueError:
        # Match the terminal-dot normalization used by the target policy.
        host_pattern = re.escape(host) + r'\.?'
    else:
        rendered = f'[{address}]' if address.version == 6 else str(address)
        host_pattern = re.escape(rendered)

    default_port = 443 if scheme == 'https' else 80
    port_pattern = (
        f'(?::{port})?'
        if port == default_port
        else f':{port}'
    )
    return (
        r'\A'
        f'(?i:{scheme}://{host_pattern})'
        f'{port_pattern}'
        r'(?:/[^\r\n]*)?\Z'
    )


def configure_scope(zap, request: ScanRequest) -> str:
    """Configure a fresh ZAP instance; do not start any target requests."""
    name = f'job-{request.job_id.hex}-{request.claim_token.hex}'
    context_id = str(zap.context.new_context(name))
    if not context_id.isdecimal():
        raise RuntimeError('Cannot create scanner scope')

    included = zap.context.include_in_context(name, request.scope_regex)
    scoped = zap.context.set_context_in_scope(name, 'true')
    if included != 'OK' or scoped != 'OK':
        raise RuntimeError('Cannot configure scanner scope')
    return name
