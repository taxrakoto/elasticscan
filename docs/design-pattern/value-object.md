# Value Object

## Status and problem

US-002.1 implements `TeamContext` in `app/core/team.py` as a frozen Python
dataclass. The resolved team and current caller belong together: passing
unrelated strings separately makes accidental substitutions easier.

A value object represents a small concept through its values. Equal
`TeamContext` objects have the same field values; they do not have a separate
database identity. Freezing the dataclass prevents ordinary field reassignment.

## Components and usage

`resolve_team_context` creates the object only after checking membership and
selection. `require_team_context` returns it through FastAPI dependency injection.
US-002.2's `create_pending_job` in `app/repositories/jobs.py` consumes this object
for ownership assignment. Future job handlers will pass the verified context:

- `team_id`: the authorized team for the current operation.
- `created_by`: the verified subject of the current caller. Job creation stores
  this as the creator; retrieving another member's job must preserve that job's
  original stored creator.

The object contains only immutable strings, with no database connection, token,
or membership collection. This makes it a small, explicit handoff between
authorization and future operations.

## Benefits, tradeoffs, and limits

Named fields make the meaning clearer than a tuple of strings. Immutability
prevents accidental reassignment during request processing. It does not provide
a security boundary against code deliberately constructing another context.

The object is not proof of authentication by itself: callers must obtain it from
the verified dependency. It does not replace database ownership constraints:
the existing PostgreSQL trigger independently rejects changes to stored ownership.
US-002.2 verifies that guard through object, bulk, and raw-SQL updates. Team-scoped
queries and actual worker processing remain subsequent work.

## Verification and requirements

`tests/unit/test_team_context.py` checks field values, value equality, and blocked
field reassignment. Signed HTTP tests and opt-in Keycloak tests verify that the
dependency returns the correct team and caller. US-002.2's PostgreSQL tests in
`tests/integration/test_job_storage.py` follow signed identity through context
resolution and repository insertion, then reload and verify persisted ownership.

This supports FR-027 and the ownership-context foundation of AC-017; full job
isolation acceptance remains pending. See [Repository](repository.md),
[Dependency Injection](dependency-injection.md) and
[Authentication and team context](../authentication.md).
