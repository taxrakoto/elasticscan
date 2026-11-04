"""Idempotent local SNS/SQS bootstrap; executed by LocalStack's ready hook."""
import json
import os

import boto3


def provision(sns, sqs):
    topic = sns.create_topic(Name='elasticscan-jobs')['TopicArn']
    dlq = sqs.create_queue(QueueName='elasticscan-zap-dlq')['QueueUrl']
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=['QueueArn'])['Attributes']['QueueArn']
    queue = sqs.create_queue(QueueName='elasticscan-zap', Attributes={
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
    sns.subscribe(TopicArn=topic, Protocol='sqs', Endpoint=queue_arn, Attributes={
        'RawMessageDelivery': 'true', 'FilterPolicy': json.dumps({'scan_type': ['zap']}),
    })
    print('Local messaging provisioned: elasticscan-jobs -> elasticscan-zap (+ DLQ)', flush=True)


if __name__ == '__main__':
    config = {'endpoint_url': 'http://localhost:4566',
              'region_name': os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')}
    provision(boto3.client('sns', **config), boto3.client('sqs', **config))
