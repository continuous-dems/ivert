"""sns.py -- Code for pushing messages to AWS SNS topics."""

import argparse
import boto3
import json
import typing

import utils.configfile
import utils.is_email

ivert_config = utils.configfile.Config()


def send_sns_message(
    subject: str, message: str, job_id: typing.Union[str, int], username: str
) -> str:
    """Send a message to an AWS SNS topic.

    Args:
        subject (str): The subject of the message.
        message (str): The body of the message.
        job_id (typing.Union[str, int]): The 12-digit numerical ID of the job.
        username (str): The username of the user who submitted the job.

    Returns:
        str: The response attributues from the SNS API, in json text format.
    """
    client = boto3.client("sns")

    topic_arn_str = topic_arn()
    assert topic_arn_str is not None

    # We could send "job_id" as a "Number" datatype but they max it out at 10^9 and our job_ids are 12 digits.
    # So, it's a string datatype.
    msg_attributes = {
        "job_id": {"DataType": "String", "StringValue": str(job_id)},
        "username": {"DataType": "String", "StringValue": username},
    }

    # Send the message.
    reply = client.publish(
        TopicArn=topic_arn_str,
        Subject=subject,
        Message=message,
        MessageAttributes=msg_attributes,
    )

    return json.dumps(reply)


def subscribe(
    email: str, username_filter: typing.Union[str, list[str], None] = None
) -> str:
    """Subscribe an email address to the IVERT AWS SNS topic.

    Args:
        email (str): The email address to subscribe.
        username_filter (str, list[str], or None): The username or list of usernames to filter on.
                    Defaults to None, which applies no filters (you'll get all the messages).

    Returns:
        A string of the subscription ARN of the new subscription just created.
    """
    client = boto3.client("sns")

    if not utils.is_email.is_email(email):
        raise ValueError(f"'{email}' is not a valid email address.")

    # Get the SNS topic ARN from the ivert_config object (which fetches it from ivert_setup/setup/paths.sh).
    topic_arn = ivert_config.sns_topic_arn
    assert topic_arn is not None

    if type(username_filter) is str:
        filter_policy = {"username": [username_filter.lower().strip()]}
    elif type(username_filter) is list:
        filter_policy = {"username": [f.lower().strip() for f in username_filter]}
    else:
        filter_policy = None

    # Send the message.
    reply = client.subscribe(
        TopicArn=topic_arn,
        Protocol="email",
        Endpoint=email,
        ReturnSubscriptionArn=True,
        Attributes={
            "FilterPolicy": json.dumps(filter_policy),
            "FilterPolicyScope": "MessageAttributes",
        }
        if filter_policy
        else {},
    )

    return reply["SubscriptionArn"]


def unsubscribe(subscription_arn: str) -> str:
    """Unsubscribe an email address from the IVERT AWS SNS topic.

    Args:
        subscription_arn (str): The subscription ARN to unsubscribe.

    Returns:
        A string of the subscription ARN of the new subscription just created.
    """
    client = boto3.client("sns")

    # Send the message.
    reply = client.unsubscribe(SubscriptionArn=subscription_arn)

    return reply


def topic_arn():
    "Return the currently-used SNS topic arn."
    return ivert_config.sns_topic_arn


def define_and_parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a message to an AWS SNS topic.")
    parser.add_argument(
        "-s",
        "--subject",
        dest="subject",
        type=str,
        required=True,
        help="The subject of the message.",
    )
    parser.add_argument(
        "-m",
        "--message",
        dest="message",
        type=str,
        required=True,
        help="The body of the message.",
    )
    parser.add_argument(
        "-j",
        "--job_id",
        dest="job_id",
        type=str,
        required=True,
        help="The 12-digit numerical ID of the job.",
    )
    parser.add_argument(
        "-u",
        "--username",
        dest="username",
        type=str,
        required=True,
        help="The username of the user who submitted the job.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()

    response = send_sns_message(args.subject, args.message, args.job_id, args.username)

    print(response)
