"""Utilities for checking on the status of a server-side job from the IVERT client, using the jobs_database."""

import argparse
import numpy
import os
import pandas
import typing
import sys

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.jobs_database as jobs_database
    import ivert_utils.configfile as configfile
    from ivert_utils.bcolors import bcolors
else:
    # If running as a script, import this way.
    import utils.configfile as configfile
    import jobs_database
    from utils.bcolors import bcolors

ivert_config = None


def find_latest_job_submitted(
    username, jobs_db: typing.Union[jobs_database.JobsDatabaseClient, None] = None
) -> str:
    """Find the most recent job submitted by this user, from either the jobs database or the local jobs directory.

    Args:
        username (str): The username to search for.
        jobs_db (jobs_database.JobsDatabaseClient or None, optional): The jobs database to use. Defaults to None.

    Returns:
        str: The most recent job name submitted by this user, from either the jobs database or the local jobs directory.
             If no jobs was found for this user, it returns "<username>_000000000000", or "__nada_000000000000" if
             no username is provided.
    """
    if jobs_db is None:
        jobs_db = jobs_database.JobsDatabaseClient()

    ##########################################################################################################
    # TODO: Use the REST API rather than the jobs database for this.
    jobs_db.download_from_s3(only_if_newer=True)
    # Read the table of all the jobs submitted by this user.
    df = jobs_db.read_table_as_pandas_df(
        "jobs", username=None if username == "" else username
    )
    job_id_from_db = df["job_id"].max()

    if job_id_from_db in [None, numpy.nan]:
        if username:
            return f"{username}_000000000000"
        else:
            return "__nada_000000000000"

    username_to_use = (
        username if username else "__nada"
    )  # __nada will be less than any actual username.
    job_name_from_db = f"{username_to_use}_{job_id_from_db}"
    ##########################################################################################################

    job_name_from_folders = get_latest_job_name_from_local_dirs()

    # Return whichever job name is greater.
    return max(job_name_from_db, job_name_from_folders)


def get_latest_job_name_from_local_dirs():
    """Find the most recent job submitted by this user on this machine, from the local jobs directory."""
    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    # Now look in the local jobs folder to see if there's a more recent job there that was submitted, but isn't yet in
    # the server's jobs database. (Perhaps it just hasn't been picked up by the server yet.)
    local_jobs_dir = ivert_config.ivert_jobs_directory_local
    job_folders = [
        fn
        for fn in os.listdir(local_jobs_dir)
        if os.path.isdir(os.path.join(local_jobs_dir, fn))
        and (ivert_config.username in fn)
    ]
    if len(job_folders) > 0:
        job_name_from_folders = max(job_folders)
    else:
        job_name_from_folders = ""

    return job_name_from_folders


def run_job_status_command(args: argparse.Namespace) -> None:
    """Run the job status command from the ivert_client."""
    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    assert hasattr(args, "job_name")
    assert hasattr(args, "command") and args.command == "status"
    assert hasattr(args, "detailed") is isinstance(args.detailed, bool)

    jobs_db = None

    if args.job_name is None or args.job_name.lower() == "latest":
        jobs_db = jobs_database.JobsDatabaseClient()
        username = ivert_config.username
        args.job_name = find_latest_job_submitted(username, jobs_db)

    if args.detailed:
        job_df, files_df = detailed_job_info(args.job_name, jobs_db)
        if len(job_df) == 0:
            print(
                f"\nJob {bcolors.OKBLUE}{bcolors.BOLD}{args.job_name}{bcolors.ENDC}{bcolors.ENDC} has not started on the IVERT server yet."
            )
            return

        print(
            f"\nJob {bcolors.OKBLUE}{bcolors.BOLD}{args.job_name}{bcolors.ENDC}{bcolors.ENDC} is {bcolors.BOLD}{repr(job_df['status'].values[0])}{bcolors.ENDC}."
        )
        input_files = files_df[files_df["import_or_export"].isin((0, 2))]
        export_files = files_df[files_df["import_or_export"].isin((1, 2))]

        if len(input_files) > 0:
            input_files_finished = (
                input_files["status"]
                .isin(("processed", "timeout", "error", "quarantined"))
                .sum()
            )
            print(
                f"{input_files_finished} of {len(input_files)} input files are finished."
            )
            print()

            print("Input file statuses:")
            for i, frow in input_files.iterrows():
                status = frow["status"]
                if status in ("downloaded", "unprocessed", "unknown"):
                    status = (
                        f"standing by {bcolors.ITALIC}(not yet processed){bcolors.ENDC}"
                    )
                elif status == "processing":
                    status = f"{bcolors.ITALIC}{bcolors.BOLD}{bcolors.OKGREEN}processing{bcolors.ENDC}{bcolors.ENDC}{bcolors.ENDC}"
                elif status == "processed":
                    status = f"{bcolors.BOLD}processed{bcolors.ENDC}"
                elif status == "timeout":
                    status = f"{bcolors.FAIL}timeout{bcolors.ENDC}"
                elif status == "error":
                    status = f"{bcolors.FAIL}error{bcolors.ENDC}"
                elif status == "quarantined":
                    status = f"{bcolors.FAIL}quarantined{bcolors.ENDC}"
                # elif status == "unknown":
                #     status = f"{bcolors.WARNING}unknown{bcolors.ENDC}"
                print(f"    {frow['filename']}: {status}", end="")
                if frow["filename"].endswith(".ini"):
                    print(f" (<-{bcolors.ITALIC}job Config file{bcolors.ENDC})")
                else:
                    print()

        if len(export_files) > 0:
            print(
                f"There are currently {len(export_files)} export files processed for this job:"
            )

            for i, frow in export_files.iterrows():
                status = frow["status"]
                if status == "uploaded":
                    status = f"{bcolors.BOLD}{status}{bcolors.ENDC}"
                elif status == "error":
                    status = f"{bcolors.FAIL}{status}{bcolors.ENDC}"
                elif status == "unknown":
                    status = f"{bcolors.WARNING}{status}{bcolors.ENDC}"

                print(f"    {frow['filename']}: {status}")

            print(
                f"\n'{bcolors.BOLD}ivert download {args.job_name}{bcolors.ENDC}' will download the results."
            )

    else:
        status = get_simple_job_status(args.job_name, jobs_db)
        if status is None:
            print(
                f"\nJob {bcolors.OKBLUE}{bcolors.BOLD}{args.job_name}{bcolors.ENDC}{bcolors.ENDC} has not started on the IVERT server yet."
            )
            # \n"
            #       "Give it a bit. If it never shows up, contact your IVERT administrator and check whether the server process is running.")
        else:
            print(
                f"\nJob {bcolors.OKBLUE}{bcolors.BOLD}{args.job_name}{bcolors.ENDC}{bcolors.ENDC} is {bcolors.BOLD}{repr(status)}{bcolors.ENDC}."
            )


def get_simple_job_status(
    job_name, jobs_db: typing.Union[jobs_database.JobsDatabaseClient, None] = None
) -> typing.Union[str, None]:
    if jobs_db is None:
        jobs_db = jobs_database.JobsDatabaseClient()

    jobs_db.download_from_s3(only_if_newer=True)

    username = job_name[: job_name.rfind("_")]
    job_id = int(job_name[job_name.rfind("_") + 1 :])

    # Fetch the job status from the database.
    return jobs_db.job_status(username, job_id)


def is_job_finished(
    job_name, jobs_db: typing.Union[jobs_database.JobsDatabaseClient, None] = None
) -> typing.Union[bool, str]:
    """Check if the job is finished.

    If it's finished, return its string status from the database.
    If not finished, return False."""
    status = get_simple_job_status(job_name, jobs_db)

    if status in ("complete", "error", "killed", "unknown"):
        return True
    else:
        assert status in ("started", "running", None)
        return False


def detailed_job_info(
    job_name, jobs_db: typing.Union[jobs_database.JobsDatabaseClient, None] = None
) -> list[pandas.DataFrame]:
    """Get detailed information about the status of the job.

    This returns 2 pandas dataframes: one with the job record from the ivert_jobs table,
    the other with the ivert_files table for that job."""
    if jobs_db is None:
        jobs_db = jobs_database.JobsDatabaseClient()

    jobs_db.download_from_s3(only_if_newer=True)

    username = job_name[: job_name.rfind("_")]
    job_id = int(job_name[job_name.rfind("_") + 1 :])

    job_record = jobs_db.read_table_as_pandas_df("jobs", username, job_id)

    files = jobs_db.read_table_as_pandas_df("files", username, job_id)

    return [job_record, files]
