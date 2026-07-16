"""Functions for managing files that have been quarantined when trying to input through the filtering system."""

import s3
import typing
import utils.configfile


def list_files_in_quarantine() -> typing.List[str]:
    """Return a list of all files that currently sit in quarantine."""
    iconfig = utils.configfile.Config()
    s3m = s3.S3Manager()

    return s3m.listdir(
        iconfig.s3_quarantine_prefix_base
        + ("" if (iconfig.s3_quarantine_prefix_base[-1] == "/") else "/")
        + "*",
        bucket_type="quarantine",
        recursive=True,
    )


def is_quarantined(s3_key: str) -> bool:
    """Return True/False if a file exists in quarantine.

    s3_key should be either the entire key, or the key without the quarantine-base directory prefix (it will be added).

    Args:
        s3_key (str): The S3 key to check.

    Returns:
        bool: True if the file exists in the quarantine bucket, False otherwise.
    """
    iconfig = utils.configfile.Config()
    s3m = s3.S3Manager()

    s3_key = s3_key.strip()
    if not s3_key.startswith(iconfig.s3_quarantine_prefix_base):
        s3_key = (iconfig.s3_quarantine_prefix_base + "/" + s3_key).replace("//", "/")

    # We're using the "listdir" routine rather than "exists" because we don't have permissions to read the header in
    # the quarantine bucket that "exists" uses. We do have permissions to "list_objects_v2" that listdir uses.
    # return s3m.exists(s3_key, bucket_type='quarantine')
    dirname = s3_key.rsplit("/", 1)[0]

    try:
        fnames = s3m.listdir(dirname, bucket_type="quarantine", recursive=False)
    except FileNotFoundError:
        # If the directory doesn't exist, it's not in quarantine (yet)
        return False

    return s3_key in fnames
