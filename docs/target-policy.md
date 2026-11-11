# Initial target policy (US-005.1)

This policy checks whether the initial scan URL is an explicitly authorized
public destination. The dependency is used by production submission since
US-003.2 and by the reusable worker handoff in the US-005.2 application phase.
It does not open
an HTTP connection to the target, follow redirects, or launch a scanner.

## Operator configuration

`TARGET_ALLOWED_ORIGINS` is a JSON array containing zero or one exact HTTP/HTTPS
origin. An origin is a scheme, host, and port. The default is `[]`, so missing configuration grants
no target access. Example syntax only:

```dotenv
TARGET_ALLOWED_ORIGINS=["https://approved.example.invalid"]
```

The example is not permission to scan a real target. Operators must obtain target
owner authorization before adding an actual origin. The allowlist is global to
the deployment; it is not a per-team target ownership registry. Any authenticated,
authorized team caller can use an approved origin.

More than one array entry is rejected as an operator configuration error. Existing
multi-origin environments must select one origin before application startup.
All paths within it are allowed; there is no prefix setting. See
[Single fixed scanner origin](scanner-scope.md) for the worker recheck, reusable
ZAP context setup and remaining real-crawl/network-enforcement requirements.

Entries may have an optional root slash, but no non-root path, query, fragment,
or credentials. Wildcards and suffix matching are not supported. Subdomains,
different schemes, and different ports require separate entries. Approval of
an origin covers all its paths and query strings, not a single URL path.

Startup validates the configuration without DNS or target connections. Invalid
JSON, a non-array value, non-string entries, invalid hosts, unsupported schemes,
or non-origin entries prevent startup. Target settings are read from the process
environment by the dependency; change deployment configuration by restarting the
API. The private local `.env` is not modified by this implementation.

## URL normalization

`ZapSubmission` uses Pydantic's `AnyHttpUrl` to validate and normalize the URL.
The policy compares the parsed origin, never a substring of the input:

- Hostnames are case-insensitive; internationalized names use the parser's IDNA
  form. One terminal DNS dot is normalized away, while malformed empty labels
  are rejected.
- The default port is 80 for HTTP and 443 for HTTPS. Explicit default ports and
  omitted defaults compare equally. Nondefault ports require explicit approval;
  port zero is rejected.
- IPv4/IPv6 literals use the parsed host. IPv6 URL brackets are removed for
  address validation. Scoped IPv6 hosts are rejected.
- Credentials and URL fragments are denied. Query strings are permitted for
  targets within an approved origin but never used to select the origin.
- Alternate IPv4 forms normalized by the URL parser still undergo address
  checks; a numeric form of loopback cannot bypass the restriction.

## Address checks and DNS

The policy checks allowlist membership before DNS, avoiding lookup for unlisted
origins. IP literals are checked directly. Hostnames use the operating system's
`socket.getaddrinfo` with `AF_UNSPEC` and `SOCK_STREAM`, collecting returned IPv4
and IPv6 addresses. Every returned address must pass; one restricted or malformed
answer denies the target. Empty results and resolution failures also deny it.

The policy requires globally reachable addresses and additionally rejects
multicast, reserved, loopback, link-local, and unspecified addresses. This excludes
private networks, shared carrier-grade NAT space, documentation/benchmark space,
and link-local metadata endpoints such as `169.254.169.254`. The Azure platform
virtual address `168.63.129.16` is explicitly excluded too.

IPv4-mapped IPv6, 6to4, Teredo, legacy site-local IPv6, scoped addresses, and the
translation prefixes `64:ff9b::/96` and `64:ff9b:1::/48` are conservatively
excluded, even when they encode a public IPv4 address. Translation can let IPv6
systems reach IPv4 destinations through a gateway; this policy supports ordinary
public IPv4 and native IPv6 instead.

Address categories come from the standard library in the pinned Python runtime,
with explicit additional exclusions above. Tests exercise the runtime actually
used by the application image. DNS uses the operating system's timeout behavior;
there is no custom resolver timeout or application DNS cache. The synchronous
FastAPI dependency runs blocking resolution in the worker thread pool.

Adding a private address to the allowlist never overrides address restrictions.
No private-network exception is added here; an isolated fixture exception belongs
to US-005.3.

## Authentication, errors, and route integration

`require_allowed_submission` first depends on `require_team_context`, preserving
JWT and team authorization, then validates the typed request's initial target.
Missing/invalid credentials return HTTP 401. Unauthorized team context returns
HTTP 403 before DNS. Invalid URL syntax or unsupported URL schemes produce the
schema's HTTP 422 response. Authenticated target-policy violations return:

```http
HTTP/1.1 403 Forbidden
Content-Type: application/json

{"detail":"Target is not allowed"}
```

The policy error includes neither URLs, query strings, credentials, DNS answers,
nor resolver diagnostics. No target details are logged by this code.

Production `POST /scan` uses the dependency before calling durable acceptance.
The following reduced example illustrates the dependency annotation:

```python
from typing import Annotated

from fastapi import Depends

from app.core.target_policy import require_allowed_submission
from app.schemas import ZapSubmission


def submit_scan(
    submission: Annotated[
        ZapSubmission,
        Depends(require_allowed_submission),
    ],
):
    # The authorized handler may now proceed to durable acceptance.
    ...
```

This example is documentation, not another endpoint. `accept_scan` remains a
trusted internal persistence service and does not itself apply target policy.
Any new submission caller must validate the target before invoking it.

## Limits and patterns

Initial DNS validation is not connection pinning. A hostname can change between
validation and worker execution, and an approved site can redirect to or discover
another destination. Redirect/crawl scope and enforceable scanner/egress controls
remain incomplete after US-005.2's single-origin application phase. Reusable scope
setup and a worker DNS recheck now exist, but real crawl verification and AWS
outbound enforcement are pending; this code does not claim complete SSRF protection
or scanner acceptance under AC-010/AC-018.

[Dependency Injection](design-pattern/dependency-injection.md) supplies the policy
to FastAPI and a controlled resolver to tests. Default deny and defense in depth
are security principles, rather than additional design patterns.

## Verification

`tests/unit/test_target_policy.py` covers exact-origin matching, normalization,
configuration, public literals, restricted address classes, mixed DNS answers,
DNS failures, and dual-family resolver behavior. The signed-JWT HTTP tests in
`tests/integration/test_target_policy.py` verify 401/403/422 behavior, successful
validation, rejection before the handler, and no DNS before authorization.
DNS is controlled; no real target is resolved or scanned by these tests.

```sh
docker run --rm --network none --read-only -v "$PWD:/workspace:ro" -w /workspace --entrypoint python elasticscan-api:local -B -m unittest tests.unit.test_target_policy tests.integration.test_target_policy -v
```

References: [OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
and [Python IPv4/IPv6 address classification](https://docs.python.org/3.12/library/ipaddress.html).
