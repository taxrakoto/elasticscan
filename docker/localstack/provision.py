"""Idempotent local SNS/SQS bootstrap; executed by LocalStack's ready hook."""
import json
import os

import boto3


def provision(
    sns,
    sqs,
    *,
    topic_name='elasticscan-jobs',
    queue_name='elasticscan-zap',
    dlq_name='elasticscan-zap-dlq',
):
    topic = sns.create_topic(Name=topic_name)['TopicArn']
    dlq = sqs.create_queue(QueueName=dlq_name)['QueueUrl']
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    queue = sqs.create_queue(QueueName=queue_name, Attributes={
        'VisibilityTimeout': '60',
        'ReceiveMessageWaitTimeSeconds': '10',
        'RedrivePolicy': json.dumps({'deadLetterTargetArn': dlq_arn, 'maxReceiveCount': '3'}),
    })['QueueUrl']
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    policy = {'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'Service': 'sns.amazonaws.com'},
        'Action': 'sqs:SendMessage', 'Resource': queue_arn,
        'Condition': {'ArnEquals': {'aws:SourceArn': topic}},
    }]}
    sqs.set_queue_attributes(QueueUrl=queue, Attributes={'Policy': json.dumps(policy)})
    subscription_attributes = {
        'RawMessageDelivery': 'true',
        'FilterPolicy': json.dumps({'scan_type': ['zap']}),
        'FilterPolicyScope': 'MessageAttributes',
    }
    subscriptions = sns.get_paginator('list_subscriptions_by_topic')
    matching = [
        item['SubscriptionArn']
        for page in subscriptions.paginate(TopicArn=topic)
        for item in page['Subscriptions']
        if item['Protocol'] == 'sqs' and item['Endpoint'] == queue_arn
    ]
    if len(matching) > 1:
        raise RuntimeError('Multiple subscriptions exist for the ZAP queue')
    if matching:
        subscription = matching[0]
    else:
        subscription = sns.subscribe(
            TopicArn=topic,
            Protocol='sqs',
            Endpoint=queue_arn,
            Attributes=subscription_attributes,
            ReturnSubscriptionArn=True,
        )['SubscriptionArn']

    for name, value in subscription_attributes.items():
        sns.set_subscription_attributes(
            SubscriptionArn=subscription,
            AttributeName=name,
            AttributeValue=value,
        )

    print(
        f'Local messaging provisioned: {topic_name} -> {queue_name} (+ DLQ)',
        flush=True,
    )
    return {
        'topic_arn': topic,
        'queue_url': queue,
        'queue_arn': queue_arn,
        'dlq_url': dlq,
        'dlq_arn': dlq_arn,
        'subscription_arn': subscription,
    }


if __name__ == '__main__':
    config = {'endpoint_url': 'http://localhost:4566',
              'region_name': os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')}
    provision(boto3.client('sns', **config), boto3.client('sqs', **config))
