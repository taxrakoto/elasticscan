"""Safe, bounded dead-letter diagnostics without external services."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.maintenance.dead_letters import (
    describe_dead_letter, main, observe_dead_letters,
)
from docker.localstack.provision import provision


class DeadLetterTests(unittest.TestCase):
    def queues(self):
        sqs = Mock()
        sqs.get_queue_attributes.side_effect = [
            {'Attributes': {
                'QueueArn': 'source-arn',
                'RedrivePolicy': json.dumps({
                    'deadLetterTargetArn': 'dlq-arn',
                }),
            }},
            {'Attributes': {
                'QueueArn': 'dlq-arn',
                'RedriveAllowPolicy': json.dumps({
                    'redrivePermission': 'byQueue',
                    'sourceQueueArns': ['source-arn'],
                }),
                'ApproximateNumberOfMessages': '1',
                'ApproximateNumberOfMessagesNotVisible': '0',
            }},
        ]
        sqs.receive_message.return_value = {'Messages': []}
        return sqs

    def test_poison_body_keeps_only_safe_correlation_fields(self):
        job_id = str(uuid4())
        message_id = str(uuid4())
        observation = describe_dead_letter({
            'Body': json.dumps({
                'schema_version': 99,
                'job_id': job_id,
                'password': 'secret-value',
                'url': 'https://private.invalid/?token=secret',
            }),
            'MessageId': message_id,
            'ReceiptHandle': 'private-receipt',
            'Attributes': {'ApproximateReceiveCount': '2'},
        })
        self.assertEqual(observation, {
            'event': 'dead_letter_observed',
            'job_id': job_id,
            'message_id': message_id,
            'schema_valid': False,
            'dlq_receive_count': 2,
        })
        self.assertNotIn('secret', json.dumps(observation))
        self.assertNotIn('receipt', json.dumps(observation))

    def test_valid_message_and_uncorrelatable_bodies(self):
        valid = describe_dead_letter({
            'Body': json.dumps({
                'schema_version': 1,
                'job_id': str(uuid4()),
                'scan_type': 'zap',
                'parameters': {'url': 'https://approved.example.invalid/'},
            }),
        })
        self.assertTrue(valid['schema_valid'])
        for body in ('{', '[]', 'null', '{"job_id":"secret\\nvalue"}'):
            with self.subTest(body=body):
                observation = describe_dead_letter({'Body': body})
                self.assertEqual(observation['job_id'], '-')
                self.assertFalse(observation['schema_valid'])

    def test_observer_receives_only_dlq_and_preserves_messages(self):
        sqs = self.queues()
        sqs.receive_message.return_value = {
            'Messages': [{'Body': '{', 'MessageId': str(uuid4())}],
        }
        with self.assertLogs('app.maintenance.dead_letters', level='INFO') as logs:
            observations = observe_dead_letters(
                sqs, source_queue_url='source', dlq_url='dlq', limit=1,
            )
        self.assertEqual(len(observations), 1)
        self.assertIn('dead_letter_depth', '\n'.join(logs.output))
        sqs.receive_message.assert_called_once_with(
            QueueUrl='dlq', MaxNumberOfMessages=1, WaitTimeSeconds=0,
            VisibilityTimeout=30,
            MessageSystemAttributeNames=['ApproximateReceiveCount'],
        )
        sqs.delete_message.assert_not_called()
        sqs.send_message.assert_not_called()
        sqs.start_message_move_task.assert_not_called()

    def test_invalid_limits_and_queue_relationship_prevent_receive(self):
        for limit in (0, 11, True, '1'):
            sqs = Mock()
            with self.assertRaises(ValueError):
                observe_dead_letters(
                    sqs, source_queue_url='source', dlq_url='dlq', limit=limit,
                )
            sqs.get_queue_attributes.assert_not_called()
            sqs.receive_message.assert_not_called()
        sqs = self.queues()
        responses = list(sqs.get_queue_attributes.side_effect)
        responses[0]['Attributes']['RedrivePolicy'] = '{}'
        sqs.get_queue_attributes.side_effect = responses
        with self.assertRaises(ValueError):
            observe_dead_letters(sqs, source_queue_url='source', dlq_url='dlq')
        sqs.receive_message.assert_not_called()

    def test_invalid_provisioning_has_no_service_side_effects(self):
        for bound in (0, 101, True, '3'):
            sns, sqs = Mock(), Mock()
            with self.assertRaises(ValueError):
                provision(sns, sqs, max_receive_count=bound)
            sns.create_topic.assert_not_called()
            sqs.create_queue.assert_not_called()
        sns, sqs = Mock(), Mock()
        with self.assertRaises(ValueError):
            provision(sns, sqs, queue_name='same', dlq_name='same')
        sns.create_topic.assert_not_called()

    def test_malformed_allow_policy_prevents_observation(self):
        sqs = self.queues()
        responses = list(sqs.get_queue_attributes.side_effect)
        responses[1]['Attributes']['RedriveAllowPolicy'] = json.dumps({
            'redrivePermission': 'byQueue', 'sourceQueueArns': None,
        })
        sqs.get_queue_attributes.side_effect = responses
        with self.assertRaises(ValueError):
            observe_dead_letters(sqs, source_queue_url='source', dlq_url='dlq')
        sqs.receive_message.assert_not_called()

    def test_cli_closes_client_on_success_and_safe_failure(self):
        for error, expected in ((None, 0), (ValueError('secret-value'), 1)):
            with self.subTest(expected=expected):
                sqs = Mock()
                with patch.dict(os.environ, {
                    'SQS_QUEUE_URL': 'source', 'SQS_DLQ_URL': 'dlq',
                }), patch('sys.argv', ['dead_letters', '--limit', '2']), patch(
                    'app.maintenance.dead_letters.boto3.client', return_value=sqs,
                ), patch(
                    'app.maintenance.dead_letters.observe_dead_letters',
                    side_effect=error, return_value=[],
                ) as observe, patch(
                    'app.maintenance.dead_letters.logging.basicConfig',
                ), patch('app.maintenance.dead_letters.logger') as logs:
                    self.assertEqual(main(), expected)
                observe.assert_called_once_with(
                    sqs, source_queue_url='source', dlq_url='dlq', limit=2,
                )
                sqs.close.assert_called_once()
                if error is not None:
                    logs.error.assert_called_once_with(
                        '{"event":"dead_letter_observation_failed"}',
                    )

    def test_cli_requires_explicit_urls_before_client_creation(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            'sys.argv', ['dead_letters'],
        ), patch(
            'app.maintenance.dead_letters.boto3.client',
        ) as client, patch(
            'app.maintenance.dead_letters.logging.basicConfig',
        ), patch('app.maintenance.dead_letters.logger'):
            self.assertEqual(main(), 1)
        client.assert_not_called()
