"""Utility for downloading results from IVERT."""

import argparse
import os
import sys

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.jobs_database as jobs_database
    import ivert.client_job_status as client_job_status
    import ivert.s3 as s3
    import ivert_utils.configfile as configfile
else:
    # If running as a script, import this way.
    import jobs_database
    import client_job_status
    import s3
    import utils.configfile as configfile

ivert_config = None


def run_download_command(args: argparse.Namespace) -> list[str]:
    """Run the ivert_client download command."""
    assert hasattr(args, "job_id_or_name")
    assert hasattr(args, "output_dir")

    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    # If we set it (by default) to just get the last job from the user, go find what the last job was.
    if args.job_id_or_name.lower() == "latest":
        args.job_id_or_name = client_job_status.find_latest_job_submitted(
            ivert_config.username
        )

    if args.job_id_or_name.find("_") == -1:
        # If it's just a numeric job ID, use that and get the username from the Config file.
        job_id = int(args.job_id_or_name)
        username = ivert_config.username

    else:
        # Otherwise, pull out the username and job_id from the job name.
        job_id = int(args.job_id_or_name[args.job_id_or_name.rfind("_") + 1 :])
        username = args.job_id_or_name[0 : args.job_id_or_name.rfind("_")]

    if args.job_dir:
        output_dir = os.path.join(
            ivert_config.ivert_jobs_directory_local, f"{username}_{job_id}"
        )
    else:
        # Get the absolute path of the output directory.
        output_dir = os.path.abspath(os.path.expanduser(args.output_dir))

    # Create the output directory if it doesn't exist.
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Download the job.
    job_name = f"{username}_{job_id}"
    d_files = download_job(job_name, output_dir)

    # Print the number of files written.
    print(len(d_files), "files written to", output_dir)

    return d_files


def find_most_recent_job_dir_from_this_machine() -> str:
    """Find the most recent job directory on this machine."""
    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    # Get the base IVERT jobs directory.
    ivert_jobs_dir = ivert_config.ivert_jobs_directory_local
    sub_folders = [
        fn
        for fn in os.listdir(ivert_jobs_dir)
        if os.path.isdir(os.path.join(ivert_jobs_dir, fn))
    ]

    return os.path.join(ivert_jobs_dir, max(sub_folders))


def find_matching_job_dir(job_name: str) -> str:
    """Find the IVERT job directory with the given name."""
    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    dirname = os.path.join(ivert_config.ivert_jobs_directory_local, job_name)
    if not os.path.exists(dirname):
        raise FileNotFoundError(
            f"Could not find directory with name {job_name} in {ivert_config.ivert_jobs_directory_local}"
        )

    return dirname


def download_job(job_name: str, dest: str) -> list[str]:
    """Download the job results from the S3 bucket."""
    # Parse the job name in the format username_jobid
    assert job_name.find("_") != -1

    username = job_name[: job_name.rfind("_")]
    job_id = int(job_name[job_name.rfind("_") + 1 :])

    # First, grab the database from the s3 bucket.
    db = jobs_database.JobsDatabaseClient()
    db.download_from_s3(
        only_if_newer=True
    )  # only_if_newer ensures we only download it if there's a newer version in the s3 bucket.

    # Get the s3 prefix for downloaded files from the database
    job_row = db.job_exists(username, job_id, return_row=True)
    if not job_row:
        raise ValueError(
            f"Could not find job with name {job_name} in the IVERT jobs database."
        )

    export_prefix = job_row["export_prefix"]
    if export_prefix is None:
        return []

    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.config()

    # In the new egress implementation, the prefix applied when exporting may not be the same as the prefix on the
    # client side after data egress. If that's the case, swap out the export_server prefix with the export_client
    # prefix.
    if ivert_config.s3_export_server_prefix_base in export_prefix:
        export_prefix = export_prefix.replace(
            ivert_config.s3_export_server_prefix_base,
            ivert_config.s3_export_client_prefix_base,
        )

    export_glob_str = export_prefix + ("*" if export_prefix[-1] == "/" else "/*")

    # Download the results
    s3m = s3.S3Manager()
    try:
        print("Downloading results from", export_glob_str, "to", dest)
        return s3m.download(
            export_glob_str, dest, bucket_type="export", show_progress_bar=True
        )
    except FileNotFoundError:
        return []


def define_and_parse_args() -> argparse.Namespace:
    """Define and parse the command line arguments for this script."""
    parser = argparse.ArgumentParser(
        description="Download job results files from IVERT."
    )
    parser.add_argument(
        "-n",
        "--job_name",
        dest="job_name",
        default=None,
        help="The name of the job to download results for. Usually in the format 'username_jobid'. "
        "Default: Download whatever the latest job was that you submitted from this machine.",
    )
    parser.add_argument(
        "-o--output_dir",
        dest="output_dir",
        default=None,
        help="The directory to download the results to. Default: Download to the .src/jobs "
        "sub-directory where the job was submitted.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()

    if args.dest is None:
        if args.job_name is None:
            # Look for the "most recent" job in your local .src/jobs directory.
            args.dest = find_most_recent_job_dir_from_this_machine()

        else:
            args.dest = find_matching_job_dir(args.job_name)

    if args.job_name is None:
        # Look for the "most recent" job in your local .src/jobs directory.
        args.job_name = os.path.basename(find_most_recent_job_dir_from_this_machine())

    download_job(args.job_name, args.dest)
