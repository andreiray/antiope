
import boto3
from botocore.exceptions import ClientError

import json
import os
import time
import datetime
from dateutil import tz
import re

from lib.account import *
from lib.common import *

import logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)

USER_RESOURCE_PATH = "iam/user"
ROLE_RESOURCE_PATH = "iam/role"

def lambda_handler(event, context):
    logger.debug("Received event: " + json.dumps(event, sort_keys=True))
    message = json.loads(event['Records'][0]['Sns']['Message'])
    logger.info("Received message: " + json.dumps(message, sort_keys=True))

    try:
        target_account = AWSAccount(message['account_id'])
        discover_roles(target_account)
        discover_users(target_account)

    except AssumeRoleError as e:
        logger.error("Unable to assume role into account {}({})".format(target_account.account_name, target_account.account_id))
        return()
    except ClientError as e:
        logger.error("AWS Error getting info for {}: {}".format(target_account.account_name, e))
        return()
    except Exception as e:
        logger.error("{}\nMessage: {}\nContext: {}".format(e, message, vars(context)))
        raise

def discover_roles(account):
    '''
        Gathers all the Route53Domains registered domains
    '''
    roles = []

    # Not all Public IPs are attached to instances. So we use ec2 describe_network_interfaces()
    # All results are saved to S3. Public IPs and metadata go to DDB (based on the the presense of PublicIp in the Association)
    iam_client = account.get_client('iam')
    response = iam_client.list_roles()
    while 'IsTruncated' in response and response['IsTruncated'] is True:  # Gotta Catch 'em all!
        roles += response['Roles']
        response = iam_client.list_roles(Marker=response['Marker']) # I love how the AWS API is so inconsistent with how they do pagination.
    roles += response['Roles']

    for role in roles:
        # The Arn tells me the account_id, but I'll do this for consistency across the rest of the resources I collect
        role['account_id']       = account.account_id
        role['account_name']     = account.account_name
        role['resource_type']    = "iam-role"
        role['last_seen']     = str(datetime.datetime.now(tz.gettz('US/Eastern')))

        # Now here is the interesting bit. What other accounts does this role trust, and do we know them?
        for s in role['AssumeRolePolicyDocument']['Statement']:
            if s['Principal'] == "*": # Dear mother of god, you're p0wned
                logger.error("Found an assume role policy that trusts everything!!!: {}".format(role_arn))
                raise GameOverManGameOverException("Found an assume role policy that trusts everything!!!: {}".format(role['Arn']))
            elif 'AWS' in s['Principal']:  # This means it's trusting an AWS Account and not an AWS Service.
                if type(s['Principal']['AWS']) is list:
                    for p in s['Principal']['AWS']:
                        process_trusted_account(p, role['Arn'])
                else:
                    process_trusted_account(s['Principal']['AWS'], role['Arn'])

        # Need to make sure the resource name is unique and service identifiable.
        resource_name = "{}-{}".format(role['RoleName'], account.account_id)
        save_resource_to_s3(ROLE_RESOURCE_PATH, resource_name, role)

def process_trusted_account(principal, role_arn):
    '''Given an AWS Principal, determine if the account is known, and if not known, add to the accounts database'''
    dynamodb = boto3.resource('dynamodb')
    account_table = dynamodb.Table(os.environ['ACCOUNT_TABLE'])

    # Principals can be an ARN, or just an account ID.
    if principal.startswith("arn"):
        account_id = principal.split(':')[4]
    elif re.match('^[0-9]{12}$', principal):
        account_id = principal
    elif principal == "*":
        logger.error("Found an assume role policy that trusts everything!!!: {}".format(role_arn))
        raise GameOverManGameOverException("Found an assume role policy that trusts everything!!!: {}".format(role_arn))
        return() # No accounts to add to the DB
    else:
        logger.error("Unable to identify what kind of AWS Principal this is: {}".format(principal))
        return()

    response = account_table.get_item(
        Key={'account_id': account_id},
        AttributesToGet=['account_id', 'account_status'],
        ConsistentRead=True
    )
    if 'Item' not in response:
        logger.info(u"Adding foreign account {}".format(account_id))
        try:
            response = account_table.put_item(
                Item={
                    'account_id'     : account_id,
                    'account_name'   : "unknown",
                    'account_status' : "FOREIGN",
                }
            )
        except ClientError as e:
            raise AccountUpdateError(u"Unable to create {}: {}".format(a[u'Name'], e))

def discover_users(account):
    '''
        Queries AWS to determine what Route53 Zones are hosted in an AWS Account
    '''
    users = []

    # Not all Public IPs are attached to instances. So we use ec2 describe_network_interfaces()
    # All results are saved to S3. Public IPs and metadata go to DDB (based on the the presense of PublicIp in the Association)
    iam_client = account.get_client('iam')
    response = iam_client.list_users()
    while 'IsTruncated' in response and response['IsTruncated'] is True:  # Gotta Catch 'em all!
        users += response['Users']
        response = iam_client.list_users(Marker=response['Marker'])
    users += response['Users']

    for user in users:
        user['account_id']       = account.account_id
        user['account_name']     = account.account_name
        user['resource_type']    = "iam-user"
        user['last_updated']     = str(datetime.datetime.now(tz.gettz('US/Eastern')))

        response = iam_client.list_mfa_devices(UserName=user['UserName'])
        if 'MFADevices' in response and len(response['MFADevices']) > 0:
            user['MFADevice'] = response['MFADevices'][0]

        # Need to make sure the resource name is unique and service identifiable.
        resource_name = "{}-{}".format(user['UserName'], account.account_id)
        save_resource_to_s3(USER_RESOURCE_PATH, resource_name, user)



def json_serial(obj):
    """JSON serializer for objects not serializable by default json code"""

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError ("Type %s not serializable" % type(obj))