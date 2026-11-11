"""Single-origin scope checks without network access or real scanning."""
import re
import unittest
from unittest.mock import Mock
from uuid import uuid4

from fastapi import HTTPException
from pydantic import AnyHttpUrl

from app.core.target_policy import TargetPolicy
from app.scanners.base import ScanRequest
from app.scanners.scope import configure_scope, fixed_origin_scope


class ScannerScopeTests(unittest.TestCase):
    def make_policy(self, answers=('8.8.8.8',)):
        return TargetPolicy(
            ['https://approved.example.invalid'],
            resolver=Mock(return_value=answers),
        )

    def test_scope_accepts_paths_and_normalized_origin(self):
        pattern = fixed_origin_scope(
            AnyHttpUrl('https://approved.example.invalid/start'),
            self.make_policy(),
        )
        for value in (
            'https://approved.example.invalid/',
            'https://approved.example.invalid/page?x=1',
            'https://APPROVED.example.invalid:443/page',
            'https://approved.example.invalid./page',
        ):
            with self.subTest(value=value):
                self.assertIsNotNone(re.fullmatch(pattern, value))

    def test_redirect_and_link_candidates_outside_origin_do_not_match(self):
        pattern = fixed_origin_scope(
            AnyHttpUrl('https://approved.example.invalid/'),
            self.make_policy(),
        )
        for value in (
            'http://approved.example.invalid/',
            'https://approved.example.invalid:8443/',
            'https://sub.approved.example.invalid/',
            'https://approved.example.invalid.attacker.invalid/',
            'https://other.example.invalid/',
            'https://user@approved.example.invalid/',
            'http://127.0.0.1/',
            'http://169.254.169.254/',
        ):
            with self.subTest(value=value):
                self.assertIsNone(re.fullmatch(pattern, value))

    def test_changed_or_mixed_private_resolution_is_denied(self):
        for answers in (
            ('127.0.0.1',),
            ('8.8.8.8', '10.0.0.1'),
            (),
        ):
            with self.subTest(answers=answers):
                with self.assertRaises(HTTPException):
                    fixed_origin_scope(
                        AnyHttpUrl('https://approved.example.invalid/'),
                        self.make_policy(answers),
                    )

    def test_empty_or_multiple_origins_cannot_create_scope(self):
        for origins in (
            [],
            ['https://first.example.invalid',
             'https://second.example.invalid'],
        ):
            with self.subTest(origins=origins):
                with self.assertRaises(ValueError):
                    fixed_origin_scope(
                        AnyHttpUrl('https://approved.example.invalid/'),
                        TargetPolicy(origins),
                    )

    def test_public_literals_and_nondefault_ports_have_exact_scope(self):
        for value in (
            'https://8.8.8.8/',
            'https://[2606:4700::1111]:8443/',
        ):
            with self.subTest(value=value):
                pattern = fixed_origin_scope(
                    AnyHttpUrl(value), TargetPolicy([value]),
                )
                self.assertIsNotNone(re.fullmatch(pattern, value))

    def request(self):
        return ScanRequest(
            job_id=uuid4(),
            url='https://approved.example.invalid/',
            claim_token=uuid4(),
            scope_regex=r'\Ahttps://approved\.example\.invalid/.*\Z',
        )

    def test_context_receives_application_scope_without_starting_scan(self):
        zap = Mock()
        zap.context.new_context.return_value = '1'
        zap.context.include_in_context.return_value = 'OK'
        zap.context.set_context_in_scope.return_value = 'OK'
        request = self.request()

        name = configure_scope(zap, request)

        zap.context.include_in_context.assert_called_once_with(
            name, request.scope_regex,
        )
        zap.context.set_context_in_scope.assert_called_once_with(name, 'true')
        zap.urlopen.assert_not_called()
        zap.spider.scan.assert_not_called()
        zap.ascan.scan.assert_not_called()

    def test_context_configuration_errors_are_rejected(self):
        for create_result, include_result, scope_result in (
            ('ERROR', 'OK', 'OK'),
            ('1', 'ERROR', 'OK'),
            ('1', 'OK', 'ERROR'),
        ):
            with self.subTest(
                create=create_result,
                include=include_result,
                scope=scope_result,
            ):
                zap = Mock()
                zap.context.new_context.return_value = create_result
                zap.context.include_in_context.return_value = include_result
                zap.context.set_context_in_scope.return_value = scope_result
                with self.assertRaises(RuntimeError):
                    configure_scope(zap, self.request())
