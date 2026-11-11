"""Default-deny validation of initial public scan targets."""
import json
import os
import socket
from collections.abc import Callable, Iterable
from ipaddress import IPv6Address, ip_address, ip_network
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import AnyHttpUrl

from app.core.team import TeamContext, require_team_context
from app.schemas import ZapSubmission


Resolver = Callable[[str, int], Iterable[str]]

TRANSLATION_NETWORKS = (
    ip_network('64:ff9b::/96'),
    ip_network('64:ff9b:1::/48'),
)


def resolve_host(host: str, port: int) -> tuple[str, ...]:
    records = socket.getaddrinfo(
        host,
        port,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return tuple(record[4][0] for record in records)


def origin(url: AnyHttpUrl) -> tuple[str, str, int]:
    if (
        url.scheme not in ('http', 'https')
        or url.username is not None
        or url.password is not None
        or url.fragment is not None
    ):
        raise ValueError('Unsupported target')

    host = url.host or ''
    if host.startswith('[') and host.endswith(']'):
        host = host[1:-1]
    host = host.removesuffix('.').lower()

    if not host or '%' in host:
        raise ValueError('Unsupported host')

    try:
        ip_address(host)
    except ValueError:
        labels = host.split('.')
        if len(host) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith('-')
            or label.endswith('-')
            or any(
                char not in 'abcdefghijklmnopqrstuvwxyz0123456789-'
                for char in label
            )
            for label in labels
        ):
            raise ValueError('Unsupported host') from None

    port = url.port
    if port is None:
        port = 443 if url.scheme == 'https' else 80
    if not 1 <= port <= 65535:
        raise ValueError('Unsupported port')

    return url.scheme, host, port


def public_address(value: str) -> bool:
    try:
        address = ip_address(value)
    except (ValueError, TypeError):
        return False

    if not address.is_global or any((
        address.is_multicast,
        address.is_reserved,
        address.is_loopback,
        address.is_link_local,
        address.is_unspecified,
    )):
        return False

    if isinstance(address, IPv6Address):
        if (
            address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
            or address.is_site_local
            or address.scope_id is not None
            or any(address in network for network in TRANSLATION_NETWORKS)
        ):
            return False

    # Azure platform virtual address; not an approved public scan destination.
    return str(address) != '168.63.129.16'


class TargetPolicy:
    def __init__(
        self,
        allowed_origins: Iterable[str] = (),
        resolver: Resolver = resolve_host,
    ):
        normalized = set()
        for value in allowed_origins:
            if not isinstance(value, str) or value != value.strip():
                raise ValueError('Invalid TARGET_ALLOWED_ORIGINS')
            url = AnyHttpUrl(value)
            if url.path not in (None, '/') or url.query is not None:
                raise ValueError('Allowlist entries must be origins')
            normalized.add(origin(url))
        self.allowed_origins = frozenset(normalized)
        self.resolver = resolver

    def validate(self, url: AnyHttpUrl) -> None:
        def deny():
            raise HTTPException(
                status_code=403,
                detail='Target is not allowed',
            )

        try:
            selected = origin(url)
        except ValueError:
            deny()

        if selected not in self.allowed_origins:
            deny()

        _, host, port = selected
        try:
            address = ip_address(host)
        except ValueError:
            try:
                addresses = tuple(self.resolver(host, port))
            except (OSError, ValueError):
                deny()
        else:
            addresses = (str(address),)

        if not addresses or not all(public_address(item) for item in addresses):
            deny()


def get_target_policy() -> TargetPolicy:
    try:
        values = json.loads(os.environ.get('TARGET_ALLOWED_ORIGINS', '[]'))
    except json.JSONDecodeError:
        raise ValueError('TARGET_ALLOWED_ORIGINS must be a JSON array') from None

    if not isinstance(values, list):
        raise ValueError('TARGET_ALLOWED_ORIGINS must be a JSON array')
    if len(values) > 1:
        raise ValueError(
            'TARGET_ALLOWED_ORIGINS accepts at most one origin'
        )
    return TargetPolicy(values)


def require_allowed_submission(
    context: Annotated[TeamContext, Depends(require_team_context)],
    submission: ZapSubmission,
    policy: Annotated[TargetPolicy, Depends(get_target_policy)],
) -> ZapSubmission:
    """Authorize identity/team before resolving a validated initial target."""
    policy.validate(submission.parameters.url)
    return submission
