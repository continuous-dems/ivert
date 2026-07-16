import boto3
import sys
from packaging.version import Version

if vars(sys.modules[__name__])["__package__"] == "ivert_utils":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.is_aws as is_aws
    import ivert_utils.configfile as configfile
else:
    try:
        # If running as a script, import this way.
        import is_aws
        import configfile
    except ModuleNotFoundError:
        # If this script is imported from another module in the src/ directory, import this way.
        import utils.is_aws as is_aws
        import utils.configfile as configfile


def fetch_min_client_from_server(ivert_config=None):
    if ivert_config is None:
        ivert_config = configfile.Config()

    if ivert_config.use_export_alt_bucket:
        profile_name = ivert_config.aws_profile_ivert_export_alt
        jobs_db_s3_key = ivert_config.s3_ivert_jobs_database_alt_client_key
        endpoint_url = ivert_config.s3_export_alt_endpoint_url
        bucket_name = ivert_config.s3_bucket_export_alt
    else:
        profile_name = ivert_config.aws_profile_ivert_export_client
        jobs_db_s3_key = ivert_config.s3_ivert_jobs_database_client_key
        endpoint_url = ivert_config.s3_export_client_endpoint_url
        bucket_name = ivert_config.s3_bucket_export_client

    # Fetch the version from the server database. Not using s3.py to avoid circular imports.
    if endpoint_url is None:
        client = boto3.Session(profile_name=profile_name).client("s3")
    else:
        client = boto3.Session(profile_name=profile_name).client(
            "s3", endpoint_url=endpoint_url
        )

    if ivert_config.ivert_export_client_use_aws_tags_instead_of_metadata:
        # If it's returned as aws tags, it'll be in a list of {"Key": key, "Value": value} dict entries.
        tagset = client.get_object_tagging(
            Bucket=str(ivert_config.s3_bucket_export_client), Key=jobs_db_s3_key
        )["TagSet"]
        tagdict = {tag["Key"]: tag["Value"] for tag in tagset}
        return tagdict[ivert_config.s3_jobs_db_min_client_version_metadata_key]
    else:
        return client.head_object(Bucket=bucket_name, Key=jobs_db_s3_key)["Metadata"][
            ivert_config.s3_jobs_db_min_client_version_metadata_key
        ]


def is_this_client_compatible():
    if is_aws.is_aws():
        raise NotImplementedError(
            "is_this_client_compatible is supported only on the AWS client. Use is_compatible instead."
        )

    ivert_config = configfile.Config()
    min_client_key = fetch_min_client_from_server(ivert_config=ivert_config)

    return Version(ivert_config.ivert_version) >= Version(min_client_key)
