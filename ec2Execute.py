import boto3
import json
from botocore.exceptions import ClientError
from utils import read_file
import paramiko
import threading
import os.path
import sys

BUCKET_NAME = "image-rec-512"
CONFIG_S3_FILE_KEY = "config/config.json"
CONFIG_LOCAL_FILE_KEY = "./config/config.json"
COMMANDS_S3_FILE_KEY = "config/commands.txt"
COMMANDS_LOCAL_FILE_KEY = "config/commands.txt"
BUCKET_INPUT_DIR = "input"
BUCKET_OUTPUT_DIR = "output"
SQS_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/103147106654/ImageRec"


def start_instance(ec2, instance_id):
    # Start an existing instance with id instance_id
    try:
        response = ec2.start_instances(InstanceIds=[instance_id], DryRun=False)
        print('Started existing instance with ID:', instance_id)
    except ClientError as e:
        print(e)


def stop_instance(ec2, instance_id):
    # Stop an instance with id instance_id
    try:
        response = ec2.stop_instances(InstanceIds=[instance_id], DryRun=False)
        print('Stopped instance with ID:', instance_id)
    except ClientError as e:
        print(e)

def start_instances(ec2_client, ec2_config, sqs_messages):
    # Start multiple instances depending on the number of messages in SQS queue
    ec2 = boto3.resource('ec2')
    thread = [0] * len(sqs_messages)
    instance_thread_link = {}

    instances = ec2.instances.filter(
        Filters=[{'Name': 'instance-state-name', 'Values': ['stopped']}])
    i = 0
    for instance in instances:
        print(instance, instance.id, instance.public_dns_name)
        instance_thread_link[i] = instance.id
        thread[i] = threading.Thread(
            target=thread_work, args=(ec2_client, ec2_config, i, instance.id, sqs_messages[i],))
        thread[i].start()
        i = i + 1
        if i == len(sqs_messages):
            break

    for j in range(len(sqs_messages)):
        if i != 0:
            thread[j].join()
            print('Stopping instance')
            stop_instance(ec2_client, instance_thread_link[j])


def delete_messages_from_sqs_queue(ec2_config, message_receipt_handle):
    # Delete messages from queue
    sqs = boto3.client('sqs', region_name=ec2_config['region'])
    print(message_receipt_handle)
    return sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=message_receipt_handle)

def add_message_to_sqs_queue(message):
    sqs = boto3.resource('sqs')
    queue = sqs.get_queue_by_name(QueueName='ImageRec')
    response = queue.send_message(MessageBody=message['Body'])

def get_messages_from_sqs_queue():
    # Queue instance which retrieves all the messages
    sqs = boto3.resource('sqs')
    queue_name = 'ImageRec'

    messages = {}
    queue = sqs.get_queue_by_name(QueueName=queue_name)

    # If wanted, set VisibilityTimeout=180
    for message in queue.receive_messages(MaxNumberOfMessages=10, WaitTimeSeconds=5, VisibilityTimeout = 10):
        body = json.loads(message.body)
        
        if(body.get('Records') == None):
            break
        # Get the message only if the message is created by the s3 instance
        if body.get('Records')[0].get('eventSource') == 'aws:s3':
            messages[message.message_id] = {
                'Id': message.message_id,
                'ReceiptHandle': message.receipt_handle,
                'Body': body
            }
    #delete_messages = list({v['Id']: v for v in delete_messages}.values())
    print('Returning {} message from SQS'.format(len(messages)))
    return list(messages.values())


def thread_work(ec2_client, ec2_config, tid, instance_id, sqs_message):
    receipt_handle = sqs_message.get('ReceiptHandle')
    delete_messages_from_sqs_queue(ec2_config, receipt_handle)
    ec2 = boto3.resource('ec2')
    start_instance(ec2_client, instance_id)
    instance = ec2.Instance(id=instance_id)
    print('Waiting for instance {} come in running state'.format(instance_id))
    instance.wait_until_running()
    print('Instance {} is now in running state'.format(instance_id))
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    privkey = paramiko.RSAKey.from_private_key_file(
        './config/image_rec_auth.pem')
    current_instance = list(ec2.instances.filter(InstanceIds=[instance_id]))
    print(current_instance[0].public_ip_address)
    print('SSH into the instance')
    ssh.connect(hostname=current_instance[0].public_ip_address,
                username='ubuntu', pkey=privkey)

    commands = get_from_local('commands')

    input_video = sqs_message['Body'].get('Records')[0].get('s3').get('object').get('key').split('/')[1]
    commands = commands.replace("inputFile", input_video)
    commands = commands.replace("outputFile", input_video+"_output.txt")
    print('\nCommannds ')
    print(commands)

    stdin, stdout, stderr = ssh.exec_command(commands)
    data = stdout.read().splitlines()
    print(data)
    for line in data:
        x = line.decode()
        print(x)

    print('error', len(stderr.read().splitlines()))
    if len(stderr.read().splitlines()) == 0:
        print('Deleting message from SQS queue')
        add_message_to_sqs_queue(sqs_message)

    ssh.close()


def check_queue_and_launch_instances(ec2_client, ec2_config):
    # Get all the messages from queue and delete it once the instances are created for each message
    messages = []

    while True:
        messages = get_messages_from_sqs_queue()

        # If there are no more messages in the queue, break
        if len(messages) == 0:
            break
        else:
            # Launch instances for each sqs message
            print('Starting instances as the SQS has {} messages'.format(len(messages)))
            start_instances(ec2_client, ec2_config, messages)


def get_from_local(file):
    # Load config from local file
    if file == 'config':
        return json.loads(read_file(CONFIG_LOCAL_FILE_KEY))
    elif file == 'commands':
        my_path = os.path.dirname(sys.argv[0])
        path = os.path.join(my_path, COMMANDS_LOCAL_FILE_KEY)
        print(path)
        file = open(path)
        return file.read()


def get_from_s3(file):
    # Load config from S3
    s3 = boto3.client('s3')
    if file == 'config':
        result = s3.get_object(Bucket=BUCKET_NAME, Key=CONFIG_S3_FILE_KEY)
        return json.loads(result["Body"].read().decode())
    elif file == 'commands':
        result = s3.get_object(Bucket=BUCKET_NAME, Key=COMMANDS_S3_FILE_KEY)
        return result


ec2_config = get_from_local('config')
ec2_client = boto3.client('ec2', region_name=ec2_config['region'])

try:
    while True:
        try:
            status = str.strip(read_file('/home/ubuntu/pi_status.txt'))
            print('Status is {}'.format(status))
            if status == 1:
                check_queue_and_launch_instances(ec2_client, ec2_config)
        except FileNotFoundError:
            print('Status file not found')
except KeyboardInterrupt:
    pass