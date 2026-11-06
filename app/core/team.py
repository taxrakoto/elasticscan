"""Resolve team context from verified identity and trusted claim mapping."""
import os
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException

from app.core.auth import require_identity


@dataclass(frozen=True)
class TeamSettings:
    claim_name: str = 'team_ids'

    def __post_init__(self):
        if (
            not isinstance(self.claim_name, str)
            or not self.claim_name.strip()
            or self.claim_name != self.claim_name.strip()
        ):
            raise ValueError(
                'OIDC_TEAM_CLAIM must be a nonempty claim name '
                'without surrounding whitespace'
            )

    @classmethod
    def from_environment(cls):
        return cls(
            claim_name=os.environ.get('OIDC_TEAM_CLAIM', 'team_ids'),
        )


@dataclass(frozen=True)
class TeamContext:
    team_id: str
    created_by: str


def get_team_settings() -> TeamSettings:
    return TeamSettings.from_environment()


def resolve_team_context(
    claims: dict[str, Any],
    settings: TeamSettings,
    selectors: list[str] | None = None,
) -> TeamContext:
    """Use only claims already verified by require_identity."""

    def forbidden():
        raise HTTPException(
            status_code=403,
            detail='Missing or unauthorized team context',
        )

    memberships = claims.get(settings.claim_name)
    if not isinstance(memberships, list) or not memberships:
        forbidden()

    if any(
        not isinstance(team, str)
        or not team.strip()
        or team != team.strip()
        for team in memberships
    ):
        forbidden()

    teams = frozenset(memberships)

    if selectors is None:
        if len(teams) != 1:
            forbidden()
        selected = next(iter(teams))
    else:
        if len(selectors) != 1:
            forbidden()
        selected = selectors[0]
        if selected not in teams:
            forbidden()

    return TeamContext(
        team_id=selected,
        created_by=claims['sub'],
    )


def require_team_context(
    claims: Annotated[dict[str, Any], Depends(require_identity)],
    settings: Annotated[TeamSettings, Depends(get_team_settings)],
    selectors: Annotated[
        list[str] | None,
        Header(alias='X-Team-ID'),
    ] = None,
) -> TeamContext:
    return resolve_team_context(claims, settings, selectors)
