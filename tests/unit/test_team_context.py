"""Team mapping and selection rules without HTTP or external services."""
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from fastapi import HTTPException

from app.core.team import (
    TeamContext,
    TeamSettings,
    get_team_settings,
    resolve_team_context,
)


class TeamContextTests(unittest.TestCase):
    def test_single_and_duplicate_membership_resolve_automatically(self):
        for memberships in (
            ['team-alpha'],
            ['team-alpha', 'team-alpha'],
        ):
            with self.subTest(memberships=memberships):
                context = resolve_team_context(
                    {'sub': 'alice', 'team_ids': memberships},
                    TeamSettings(),
                )
                self.assertEqual(context, TeamContext('team-alpha', 'alice'))

    def test_explicit_selection_uses_verified_membership(self):
        for memberships in (
            ['team-alpha'],
            ['team-alpha', 'team-beta'],
        ):
            with self.subTest(memberships=memberships):
                context = resolve_team_context(
                    {'sub': 'alice', 'team_ids': memberships},
                    TeamSettings(),
                    ['team-alpha'],
                )
                self.assertEqual(context.team_id, 'team-alpha')
                self.assertEqual(context.created_by, 'alice')

    def test_invalid_context_is_denied_uniformly(self):
        cases = [
            ({}, None),
            ({'team_ids': None}, None),
            ({'team_ids': []}, None),
            ({'team_ids': 'team-alpha'}, None),
            ({'team_ids': {'team-alpha': True}}, None),
            ({'team_ids': [123]}, None),
            ({'team_ids': [True]}, None),
            ({'team_ids': ['']}, None),
            ({'team_ids': [' team-alpha']}, None),
            ({'team_ids': ['team-alpha', None]}, ['team-alpha']),
            ({'team_ids': ['team-alpha', 'team-beta']}, None),
            ({'team_ids': ['team-alpha']}, ['team-beta']),
            ({'team_ids': ['team-alpha']}, ['']),
            ({'team_ids': ['team-alpha']}, ['team-alpha ']),
            ({'team_ids': ['team-alpha']}, []),
            ({'team_ids': ['team-alpha']}, ['team-alpha', 'team-alpha']),
        ]
        for claims, selectors in cases:
            with self.subTest(claims=claims, selectors=selectors):
                with self.assertRaises(HTTPException) as error:
                    resolve_team_context(
                        {'sub': 'alice', **claims}, TeamSettings(), selectors,
                    )
                self.assertEqual(error.exception.status_code, 403)
                self.assertEqual(
                    error.exception.detail,
                    'Missing or unauthorized team context',
                )

    def test_configured_claim_has_no_fallback(self):
        settings = TeamSettings('memberships')
        context = resolve_team_context(
            {
                'sub': 'alice',
                'memberships': ['team-beta'],
                'team_ids': ['team-alpha'],
            },
            settings,
        )
        self.assertEqual(context.team_id, 'team-beta')

        with self.assertRaises(HTTPException):
            resolve_team_context(
                {'sub': 'alice', 'team_ids': ['team-alpha']}, settings,
            )

    def test_environment_mapping_and_invalid_configuration(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(get_team_settings(), TeamSettings())

        with patch.dict(
            'os.environ', {'OIDC_TEAM_CLAIM': 'memberships'}, clear=True,
        ):
            self.assertEqual(get_team_settings(), TeamSettings('memberships'))

        for value in ('', ' ', ' team_ids', 'team_ids '):
            with self.subTest(value=value):
                with patch.dict(
                    'os.environ', {'OIDC_TEAM_CLAIM': value}, clear=True,
                ):
                    with self.assertRaises(ValueError):
                        get_team_settings()

    def test_context_is_immutable(self):
        context = TeamContext('team-alpha', 'alice')
        with self.assertRaises(FrozenInstanceError):
            context.team_id = 'team-beta'
