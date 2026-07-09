import os


def is_aws():
    "Return True if running in an Amazon Web Services environment. False otherwise."
    # The Amazon OS 2 EC2 instances we run have a /var/lib/cloud/instance/datasource file,
    # which contains "DataSourceEc2: DataSourceEc2" line. Look for that.
    datasource_path = "/var/lib/cloud/instance/datasource"
    # TODO: Get this to run in Windows too

    # This logic is for checking for an EC2 instance. We may need to look for certain environment variables if we're
    # running in AWS Lambda functions, or similar. Cross that bridge when it comes.
    try:
        return os.path.exists(datasource_path) and "DataSourceEc2" in open(datasource_path, 'r').read()

    except (NameError, FileNotFoundError):
        # During process shutdown, as the process is no longer running we can hit an error here. Just return False if
        # if that happens.
        return False


if __name__ == "__main__":
    print(is_aws())
