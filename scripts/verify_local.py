"""Bootstrap checks; run as python -m scripts.verify_local from the project root.

Does not claim team authorization or scan acceptance.
"""
import json
import os
import time
import uuid
from dataclasses import replace
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
import jwt
import psycopg

from app.core.auth import OIDCSettings, TokenValidator


def load_json(url, data=None):
    with urlopen(Request(url, data=data), timeout=10) as response:
        return json.load(response)


def verify_identity():
    settings = OIDCSettings.from_environment()
    validator = TokenValidator(settings)
    teams = {}
    for username in ('alice', 'alice-peer', 'bob'):
        data = urlencode({'grant_type': 'password', 'client_id': 'elasticscan-dev',
                          'username': username, 'password': os.environ['LOCAL_USER_PASSWORD']}).encode()
        token = load_json('http://keycloak:8080/realms/elasticscan/protocol/openid-connect/token', data)['access_token']
        claims = validator.validate(token)
        teams[username] = claims[os.environ['OIDC_TEAM_CLAIM']]
        prefix, signature = token.rsplit('.', 1)
        corrupted = prefix + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:]
        try:
            validator.validate(corrupted)
        except jwt.InvalidTokenError:
            pass
        else:
            raise AssertionError('A corrupted signature was accepted')
        for override in ({'audience': 'wrong-api'}, {'issuer': 'http://untrusted.invalid'}):
            try:
                TokenValidator(replace(settings, **override)).validate(token)
            except jwt.InvalidTokenError:
                pass
            else:
                raise AssertionError('Invalid identity configuration was accepted')
    assert teams == {'alice': ['team-alpha'], 'alice-peer': ['team-alpha'], 'bob': ['team-beta']}, teams
    discovery = load_json('http://keycloak:8080/realms/elasticscan/.well-known/openid-configuration')
    assert discovery['issuer'] == os.environ['OIDC_ISSUER']
    print('PASS: signed RS256 tokens, issuer, audience, and trusted membership for two teams')


def verify_messaging():
    # Guard against accidentally publishing bootstrap fixtures to real AWS.
    if os.environ['SNS_ENDPOINT_URL'] != 'http://localstack:4566' or os.environ['SQS_ENDPOINT_URL'] != 'http://localstack:4566':
        raise SystemExit('Bootstrap verification requires the default LocalStack endpoints')
    sns = boto3.client('sns', endpoint_url=os.environ['SNS_ENDPOINT_URL'])
    sqs = boto3.client('sqs', endpoint_url=os.environ['SQS_ENDPOINT_URL'])
    queue = os.environ['SQS_QUEUE_URL']
    topic = os.environ['SNS_TOPIC_ARN']
    attrs = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=['All'])['Attributes']
    policy = json.loads(attrs['Policy'])['Statement'][0]
    assert policy['Principal'] == {'Service': 'sns.amazonaws.com'}
    assert policy['Action'] == 'sqs:SendMessage'
    assert policy['Resource'] == attrs['QueueArn']
    assert policy['Condition']['ArnEquals']['aws:SourceArn'] == topic
    assert int(json.loads(attrs['RedrivePolicy'])['maxReceiveCount']) == 3
    subscriptions = sns.list_subscriptions_by_topic(TopicArn=topic)['Subscriptions']
    matching = [s for s in subscriptions if s['Endpoint'] == attrs['QueueArn']]
    assert len(matching) == 1, 'Provisioning must not duplicate subscriptions'
    subscription = matching[0]
    sub_attrs = sns.get_subscription_attributes(SubscriptionArn=subscription['SubscriptionArn'])['Attributes']
    assert json.loads(sub_attrs['FilterPolicy']) == {'scan_type': ['zap']}
    assert sub_attrs['FilterPolicyScope'] == 'MessageAttributes'
    assert sub_attrs['RawMessageDelivery'] == 'true'
    marker = str(uuid.uuid4())
    for scan_type in ('zap', 'unrelated'):
        sns.publish(TopicArn=topic, Message=json.dumps({'bootstrap_check': marker, 'scan_type': scan_type}),
                    MessageAttributes={'scan_type': {'DataType': 'String', 'StringValue': scan_type}})
    received = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for message in sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10, WaitTimeSeconds=2).get('Messages', []):
            body = json.loads(message['Body'])
            if body.get('bootstrap_check') == marker:
                received.append(body['scan_type'])
                sqs.delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
    assert received and set(received) == {'zap'}, received
    print('PASS: SNS/SQS endpoints, source policy, DLQ configuration, and scan_type routing')


def main():
    with psycopg.connect('') as connection:
        assert connection.execute('SELECT 1').fetchone() == (1,)
    print('PASS: PostgreSQL connection')
    assert load_json('http://api:8000/health/live')['mode'] == 'bootstrap'
    assert '/health/live' in load_json('http://api:8000/openapi.json')['paths']
    print('PASS: API liveness and OpenAPI')
    verify_identity()
    verify_messaging()
    print('Bootstrap checks passed. Protected scan routes, transactional acceptance, and team isolation await their feature stories.')


if __name__ == '__main__':
    main()
