"""fetch_text.py -- utilities for getting certain text fields from files or strings."""

import re
import typing


def fetch_email_address(text: str,
                        fetch_all: bool = False) -> typing.Union[str, None, list[str]]:
    """Get a valid email address from a string.

    Args:
        text (str): The string to search for email addresses.
        fetch_all (bool, optional): Whether to fetch all email addresses in the string.
                                    Defaults to False (just return the first one)..

    Returns:
        None if no email addresses are found in the string.
        str if an email address is found in the string.
        list[str] if fetch_all is True. Could be an empty list if no email addresses are found."""

    email_regex = r"(?<![a-zA-Z0-9_.])[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]*[a-zA-Z0-9]+(?![a-zA-Z0-9_])"
    return _fetch_text(text, email_regex, fetch_all=fetch_all)


def fetch_aws_bucketname(text: str,
                         fetch_all: bool = False) -> typing.Union[str, None, list[str]]:
    """Get an AWS bucket name from a string.

    Args:
        text (str): The string to search for an AWS bucket name.
        fetch_all (bool, optional): Whether to fetch all AWS bucket names in the string.
                                    Defaults to False (just return the first one).

    Returns:
        None if no AWS bucket name is found in the string.
        str if an AWS bucket name is found in the string.
        list[str] if fetch_all is True. Could be an empty list if no AWS bucket names are found.
    """

    # Bucket naming rules from https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html
    bucket_name_regex = r'^((?!xn--)(?!.*-s3alias$)[a-z0-9][a-z0-9-]{1,61}[a-z0-9])$'
    return _fetch_text(text, bucket_name_regex, fetch_all=fetch_all)


def fetch_access_key_id(text: str,
                        fetch_all: bool = False) -> typing.Union[str, None, list[str]]:
    """Get an AWS access key ID from a string.

    Args:
        text (str): The string to search for an AWS access key ID.
        fetch_all (bool, optional): Whether to fetch all AWS access key IDs in the string.
                                    Defaults to False (just return the first one).

    Returns:
        None if no AWS access key ID is found in the string.
        str if an AWS access key ID is found in the string.
        list[str] if fetch_all is True. Could be an empty list if no AWS access key IDs are found.
    """
    # Access key ID rules from https://aws.amazon.com/blogs/security/a-safer-way-to-distribute-aws-credentials-to-ec2/
    access_key_id_regex = re.compile(r'(?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9])')
    return _fetch_text(text, access_key_id_regex, fetch_all=fetch_all)


def fetch_secret_access_key(text: str,
                            fetch_all: bool = False) -> typing.Union[str, None, list[str]]:
    """Get an AWS secret access key from a string.

    Args:
        text (str): The string to search for an AWS secret access key.
        fetch_all (bool, optional): Whether to fetch all AWS secret access keys in the string.
                                    Defaults to False (just return the first one).

    Returns:
        None if no AWS secret access key is found in the string.
        str if an AWS secret access key is found in the string.
        list[str] if fetch_all is True. Could be an empty list if no AWS access key IDs are found.
    """
    # Secret access key rules from https://aws.amazon.com/blogs/security/a-safer-way-to-distribute-aws-credentials-to-ec2/
    secret_access_key_regex = re.compile(r'(?<![A-Za-z0-9/+=])[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])')
    return _fetch_text(text, secret_access_key_regex, fetch_all=fetch_all)


def _fetch_text(text: str,
                regex: str,
                fetch_all: bool = False) -> typing.Union[str, None, list[str]]:
    """Fetch text from a string. A generic function that the others use.

    Args:
        text (str): The string to search for text.
        regex (str): The regular expression to use to search for text.
        fetch_all (bool, optional): Whether to fetch all text in the string.
                                    Defaults to False (just return the first one).

    Returns:
        None if no text is found in the string.
        str if a text is found in the string.
        list[str] if fetch_all is True. Could be an empty list if no text is found.
    """
    if fetch_all:
        # Find all AWS bucket names in the string
        return re.findall(regex, text)
    else:
        # Find the first AWS bucket name in the string
        bucket_name = re.search(regex, text)
        if bucket_name:
            return bucket_name.group()
        else:
            return
