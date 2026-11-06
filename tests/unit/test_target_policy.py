"""Initial target policy without external DNS or target connections."""
import socket
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pydantic import AnyHttpUrl

from app.core.target_policy import TargetPolicy, get_target_policy, resolve_host


class TargetPolicyTests(unittest.TestCase):
    def assert_denied(self, policy, value):
        with self.assertRaises(HTTPException) as error:
            policy.validate(AnyHttpUrl(value))
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(error.exception.detail, 'Target is not allowed')

    def test_approved_origin_accepts_public_ipv4_and_ipv6(self):
        resolver = Mock(return_value=('8.8.8.8', '2606:4700:4700::1111'))
        policy = TargetPolicy(['https://approved.example.invalid'], resolver)
        policy.validate(
            AnyHttpUrl('https://APPROVED.example.invalid./path?mode=baseline')
        )
        resolver.assert_called_once_with('approved.example.invalid', 443)

    def test_unlisted_origins_are_denied_without_dns(self):
        resolver = Mock()
        policy = TargetPolicy(['https://approved.example.invalid'], resolver)
        for value in (
            'https://other.example.invalid/',
            'https://sub.approved.example.invalid/',
            'https://approved.example.invalid.attacker.invalid/',
            'http://approved.example.invalid/',
            'https://approved.example.invalid:8443/',
            'https://user:password@approved.example.invalid/',
            'https://approved.example.invalid/#section',
            'https://approved.example.invalid../',
            'https://approved.example.invalid:0/',
        ):
            with self.subTest(value=value):
                self.assert_denied(policy, value)
        resolver.assert_not_called()

    def test_restricted_literals_remain_denied_when_allowlisted(self):
        addresses = (
            '127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.0.1',
            '169.254.169.254', '169.254.170.2', '100.100.100.200',
            '0.0.0.0', '192.0.2.1', '198.18.0.1', '224.0.0.1',
            '255.255.255.255', '168.63.129.16', '::', '::1', 'fc00::1',
            'fe80::1', 'fec0::1', 'ff02::1', '2001:db8::1',
            '::ffff:127.0.0.1', '::ffff:8.8.8.8', '64:ff9b::7f00:1',
            '2002:7f00:1::', '64:ff9b::808:808', '64:ff9b:1::808:808',
            '2002:0808:0808::', '2001:0:4136:e378:8000:63bf:3fff:fdd2',
        )
        for address in addresses:
            host = f'[{address}]' if ':' in address else address
            value = f'http://{host}/'
            with self.subTest(address=address):
                self.assert_denied(TargetPolicy([value]), value)

        self.assert_denied(
            TargetPolicy(['http://127.0.0.1']), 'http://2130706433/',
        )

    def test_approved_public_literals_do_not_use_dns(self):
        resolver = Mock()
        values = (
            'https://8.8.8.8/', 'https://[2606:4700:4700::1111]/',
            'http://8.8.8.8/', 'http://8.8.8.8:8080/',
        )
        policy = TargetPolicy(values, resolver)
        for value in values:
            policy.validate(AnyHttpUrl(value))
        resolver.assert_not_called()

    def test_any_restricted_dns_answer_denies_entire_target(self):
        origin = 'https://approved.example.invalid'
        answers = (
            (), ('127.0.0.1',), ('8.8.8.8', '10.0.0.1'),
            ('8.8.8.8', '::1'), ('8.8.8.8', 'not-an-address'),
        )
        for result in answers:
            with self.subTest(result=result):
                self.assert_denied(
                    TargetPolicy([origin], Mock(return_value=result)), origin,
                )

    def test_dns_failure_is_a_safe_denial(self):
        resolver = Mock(side_effect=socket.gaierror('fixture failure'))
        self.assert_denied(
            TargetPolicy(['https://approved.example.invalid'], resolver),
            'https://approved.example.invalid/path?private=value',
        )

    def test_configuration_is_default_deny_and_rejects_invalid_values(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assert_denied(
                get_target_policy(), 'https://approved.example.invalid/',
            )

        invalid = (
            '', '*', '{}', '"https://approved.example.invalid"', '[null]',
            '["https://*.example.invalid"]',
            '["https://approved.example.invalid/path"]',
            '["https://user@approved.example.invalid"]',
            '["https://approved.example.invalid?x=1"]',
            '["ftp://approved.example.invalid"]',
            '["https://approved.example.invalid../"]',
            '["https://approved.example.invalid:0/"]',
        )
        for value in invalid:
            with self.subTest(value=value):
                with patch.dict(
                    'os.environ', {'TARGET_ALLOWED_ORIGINS': value}, clear=True,
                ):
                    with self.assertRaises(ValueError):
                        get_target_policy()

        with patch.dict(
            'os.environ',
            {'TARGET_ALLOWED_ORIGINS': '["https://approved.example.invalid"]'},
            clear=True,
        ):
            policy = get_target_policy()
            self.assertEqual(
                policy.allowed_origins,
                frozenset({('https', 'approved.example.invalid', 443)}),
            )

    def test_system_resolver_collects_both_address_families(self):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (
                socket.AF_INET6, socket.SOCK_STREAM, 6, '',
                ('2606:4700:4700::1111', 443, 0, 0),
            ),
        ]
        with patch('socket.getaddrinfo', return_value=records) as lookup:
            self.assertEqual(
                resolve_host('approved.example.invalid', 443),
                ('8.8.8.8', '2606:4700:4700::1111'),
            )
        lookup.assert_called_once_with(
            'approved.example.invalid', 443,
            family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
        )
