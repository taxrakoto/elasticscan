"""Opt-in dedicated routing checks against the local SNS/SQS emulator."""
import json
import os
import time
import unittest
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from docker.localstack.provision import provision


@unittest.skipUnless(
    os.environ.get('RUN_LOCALSTACK_TESTS') == '1',
    'Set RUN_LOCALSTACK_TESTS=1 for local SNS/SQS tests',
)
class QueueRoutingTests(unittest.TestCase):
    def setUp(self):
        endpoint = 'http://localstack:4566'
        if (
            os.environ.get('SNS_ENDPOINT_URL') != endpoint
            or os.environ.get('SQS_ENDPOINT_URL') != endpoint
        ):
            raise RuntimeError(
                'Routing fixture tests require default LocalStack endpoints',
            )

        config = Config(
            connect_timeout=3,
            read_timeout=5,
            retries={'max_attempts': 1},
        )
        client_options = {
            'endpoint_url': endpoint,
            'region_name': 'us-east-1',
            'aws_access_key_id': 'test',
            'aws_secret_access_key': 'test',
            'config': config,
        }
        self.sns = boto3.client('sns', **client_options)
        self.sqs = boto3.client('sqs', **client_options)
        self.addCleanup(self.sns.close)
        self.addCleanup(self.sqs.close)

        prefix = 'test-routing-' + uuid4().hex
        self.names = {
            'topic_name': prefix + '-jobs',
            'queue_name': prefix + '-zap',
            'dlq_name': prefix + '-dlq',
        }

        topic = self.sns.create_topic(
            Name=self.names['topic_name'],
        )['TopicArn']
        self.addCleanup(self.sns.delete_topic, TopicArn=topic)
        self.addCleanup(self.delete_queue, self.names['dlq_name'])
        self.addCleanup(self.delete_queue, self.names['queue_name'])

        self.resources = provision(self.sns, self.sqs, **self.names)

    def delete_queue(self, name):
        try:
            queue = self.sqs.get_queue_url(QueueName=name)['QueueUrl']
        except ClientError as error:
            if error.response['Error']['Code'] in (
                'AWS.SimpleQueueService.NonExistentQueue',
                'QueueDoesNotExist',
            ):
                return
            raise
        self.sqs.delete_queue(QueueUrl=queue)

    def subscription_attributes(self):
        return self.sns.get_subscription_attributes(
            SubscriptionArn=self.resources['subscription_arn'],
        )['Attributes']

    def test_queue_policy_and_subscription_configuration(self):
        resources = self.resources
        attributes = self.sqs.get_queue_attributes(
            QueueUrl=resources['queue_url'],
            AttributeNames=['All'],
        )['Attributes']

        self.assertEqual(
            json.loads(attributes['Policy']),
            {
                'Version': '2012-10-17',
                'Statement': [{
                    'Effect': 'Allow',
                    'Principal': {'Service': 'sns.amazonaws.com'},
                    'Action': 'sqs:SendMessage',
                    'Resource': resources['queue_arn'],
                    'Condition': {
                        'ArnEquals': {
                            'aws:SourceArn': resources['topic_arn'],
                        },
                    },
                }],
            },
        )
        self.assertEqual(attributes['VisibilityTimeout'], '60')
        self.assertEqual(attributes['ReceiveMessageWaitTimeSeconds'], '10')

        redrive = json.loads(attributes['RedrivePolicy'])
        self.assertEqual(
            redrive['deadLetterTargetArn'], resources['dlq_arn'],
        )
        self.assertEqual(int(redrive['maxReceiveCount']), 3)

        subscription = self.subscription_attributes()
        self.assertEqual(
            json.loads(subscription['FilterPolicy']),
            {'scan_type': ['zap']},
        )
        self.assertEqual(
            subscription['FilterPolicyScope'], 'MessageAttributes',
        )
        self.assertEqual(subscription['RawMessageDelivery'], 'true')

    def test_reprovisioning_restores_subscription_without_duplicates(self):
        subscription = self.resources['subscription_arn']
        changed = {
            'FilterPolicy': json.dumps({'scan_type': ['unrelated']}),
            'FilterPolicyScope': 'MessageBody',
            'RawMessageDelivery': 'false',
        }
        for name, value in changed.items():
            self.sns.set_subscription_attributes(
                SubscriptionArn=subscription,
                AttributeName=name,
                AttributeValue=value,
            )

        repeated = provision(self.sns, self.sqs, **self.names)
        self.assertEqual(repeated, self.resources)

        subscriptions = self.sns.list_subscriptions_by_topic(
            TopicArn=self.resources['topic_arn'],
        )['Subscriptions']
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(
            subscriptions[0]['Endpoint'], self.resources['queue_arn'],
        )

        attributes = self.subscription_attributes()
        self.assertEqual(
            json.loads(attributes['FilterPolicy']),
            {'scan_type': ['zap']},
        )
        self.assertEqual(
            attributes['FilterPolicyScope'], 'MessageAttributes',
        )
        self.assertEqual(attributes['RawMessageDelivery'], 'true')

    def test_routing_uses_attributes_and_preserves_raw_body(self):
        accepted = []
        cases = (
            ('zap', 'unrelated', True),
            ('unrelated', 'zap', False),
            (None, 'zap', False),
        )
        for attribute, body_type, expected in cases:
            body = json.dumps({
                'schema_version': 1,
                'job_id': str(uuid4()),
                'scan_type': body_type,
                'parameters': {
                    'url': 'https://approved.example.invalid/',
                },
            })
            request = {
                'TopicArn': self.resources['topic_arn'],
                'Message': body,
            }
            if attribute is not None:
                request['MessageAttributes'] = {
                    'scan_type': {
                        'DataType': 'String',
                        'StringValue': attribute,
                    },
                }
            self.sns.publish(**request)
            if expected:
                accepted.append(body)

        received = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            messages = self.sqs.receive_message(
                QueueUrl=self.resources['queue_url'],
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
            ).get('Messages', [])
            for message in messages:
                received.append(message['Body'])
                self.sqs.delete_message(
                    QueueUrl=self.resources['queue_url'],
                    ReceiptHandle=message['ReceiptHandle'],
                )

        self.assertTrue(received, 'The ZAP routing attribute was not delivered')
        self.assertEqual(set(received), set(accepted))


if __name__ == '__main__':
    unittest.main()
