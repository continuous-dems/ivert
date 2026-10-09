"""Detect whether IVERT is running on an Amazon Web Services EC2 instance."""

from pathlib import Path


def is_aws():
    """Return True if running in an Amazon Web Services environment. False otherwise."""
    # The Amazon OS 2 EC2 instances we run have a /var/lib/cloud/instance/datasource
    # file, which contains "DataSourceEc2: DataSourceEc2" line. Look for that.
    datasource_path = Path("/var/lib/cloud/instance/datasource")

    # This logic is for checking for an EC2 instance. We may need to look for certain
    # environment variables if we're running in AWS Lambda functions, or similar. Cross
    # that bridge when it comes.
    try:
        return (
            datasource_path.exists()
            and "DataSourceEc2" in datasource_path.read_text(encoding="utf-8")
        )

    except (NameError, FileNotFoundError):
        # During process shutdown, as the process is no longer running we can hit an
        # error here. Just return False if if that happens.
        return False
