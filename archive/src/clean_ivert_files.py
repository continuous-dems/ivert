"""Quick utility for cleaning out job files, database, and cache directory to free up disk space."""

import argparse
import dateparser
import os
import psutil
import re
import shutil
import typing

import utils.configfile
import utils.traverse_directory
import jobs_database
import s3
import ivert_jobs


def clean_cudem_cache(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    only_if_no_running_jobs: bool = True,
    verbose: bool = True,
):
    """Clean out the .cudem_cache directory.

    This should be run server-side only."""
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    # First check to see if any active IVERT jobs are running. If so, we don't want to clean out the cache.
    if only_if_no_running_jobs:
        try:
            jobs_db = jobs_database.JobsDatabaseServer()
            # Check if any of the jobs are still running.
            running_jobs = [
                job
                for job in jobs_db.list_unfinished_jobs(return_rows=True)
                if psutil.pid_exists(job["job_pid"])
            ]
            for running_job in running_jobs:
                if ivert_jobs.is_pid_an_active_ivert_job(running_job["job_pid"]):
                    if verbose:
                        print(
                            f"Skipping .cudem_cache cleanup for job {running_job['job_id']}, as it is still running."
                        )
                    return
        except FileNotFoundError:
            pass

    cache_dir = os.path.join(ivert_config.cudem_cache_directory, ".cudem_cache")
    shutil.rmtree(cache_dir, ignore_errors=True)

    if not os.path.exists(cache_dir):
        os.mkdir(cache_dir)

    return


def disk_usage_pct(ivert_config: typing.Union[utils.configfile.Config, None] = None):
    """Calculate the disk usage percentage of the ivert data directory."""
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    data_dir = ivert_config.ivert_data_directory

    return psutil.disk_usage(data_dir).percent


def fix_database_of_orphaned_jobs():
    """Fix the job status of any orphaned jobs that are no longer running on the server."""
    jobs_db = jobs_database.JobsDatabaseServer()

    try:
        open_jobs = [job for job in jobs_db.list_unfinished_jobs(return_rows=True)]

        for job in open_jobs:
            # For any jobs that are still 'unfinished' on the server but don't map to any active IVERT processes,
            # mark them as 'error'
            if (not psutil.pid_exists(job["job_pid"])) or (
                not ivert_jobs.is_pid_an_active_ivert_job(job["job_pid"])
            ):
                jobs_db.update_job_status(
                    job["username"], job["job_id"], "error", upload_to_s3=False
                )

        jobs_db.upload_to_s3(only_if_newer=True)
    except FileNotFoundError:
        pass


def clean_old_jobs_dirs(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    verbose: bool = True,
):
    """Clean out old job directories local.

    This can be run client-side or server-side."""
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    if ivert_config.is_aws:
        jobs_files = utils.traverse_directory.list_files(
            ivert_config.ivert_jobs_directory_local
        )
        jobs_db = jobs_database.JobsDatabaseServer()

        jobs_df = jobs_db.read_table_as_pandas_df("jobs")
        for i, job_row in jobs_df.iterrows():
            job_pid = job_row["job_pid"]

            if ivert_jobs.is_pid_an_active_ivert_job(job_pid):
                continue

            job_datadir = job_row["input_dir_local"]
            if os.path.exists(job_datadir):
                shutil.rmtree(job_datadir)

            job_ini_file = job_row["configfile"]
            stdout_file = os.path.join(
                ivert_config.ivert_jobs_stdout_dir,
                os.path.splitext(job_ini_file)[0] + "_stdout.txt",
            )
            if os.path.exists(stdout_file):
                os.remove(stdout_file)

    else:
        jobs_subdirs = sorted(
            [
                os.path.join(ivert_config.ivert_jobs_directory_local, job_dir)
                for job_dir in os.listdir(ivert_config.ivert_jobs_directory_local)
                if os.path.isdir(
                    os.path.join(ivert_config.ivert_jobs_directory_local, job_dir)
                )
            ]
        )

        jobs_db = jobs_database.JobsDatabaseClient()
        jobs_db.download_from_s3(only_if_newer=True)
        running_jobs_list = jobs_db.list_unfinished_jobs()

        for job_dir in jobs_subdirs:
            job_name = os.path.split(job_dir)[-1].strip(os.sep)
            job_username = job_name.rsplit("_", 1)[0]
            job_id = job_name.rsplit("_", 1)[-1]

            # Remove any jobs that are no longer running
            if [
                job
                for job in running_jobs_list
                if job["username"] == job_username and job["job_id"] == job_id
            ]:
                shutil.rmtree(job_dir)


def truncate_jobs_database(date_cutoff_str: str = "7 days ago", verbose: bool = True):
    """Archive the IVERT jobs database to store only very-recent jobs & files.

    Old records will be archived on the server. This should only be run on the server side.

    Args:
        date_cutoff_str (str, optional): The date cutoff to use. Defaults to "7 days ago".
        verbose (bool, optional): Whether to print verbose output. Defaults to True.
    """
    try:
        jobs_database.JobsDatabaseServer().archive_database(
            date_cutoff_str, verbose=verbose
        )
    except FileNotFoundError:
        pass


def clean_export_dirs(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    date_cutoff_str: str = "7 days ago",
    verbose: bool = True,
):
    """Clean out old files from the "export" bucket in the Secure Ingest pipeline.

    This should be run from the server side.

    Args:
        ivert_config (typing.Union[utils.configfile.Config, None], optional): The IVERT Config to use. Defaults to None.
        date_cutoff_str (str, optional): The date cutoff to use. Defaults to "7 days ago".
        verbose (bool, optional): Whether to print verbose output. Defaults to True.
    """
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    cutoff_datetime = dateparser.parse(date_cutoff_str)

    cutoff_job_number = int(
        f"{cutoff_datetime.year}{cutoff_datetime.month:02d}{cutoff_datetime.day:02d}0000"
    )

    s3m = s3.S3Manager()
    key_list = s3m.listdir(
        ivert_config.s3_export_prefix_base, bucket_type="export", recursive=True
    )

    # Find all job IDs in the keys
    job_id_regex = re.compile(r"(?<=/)\d{12}(?=/)")

    numfiles_deleted = 0
    for key in key_list:
        result = job_id_regex.search(key)
        if result is None:
            continue

        job_id = int(result.group(0))

        if job_id < cutoff_job_number:
            s3m.delete(key, bucket_type="export_server")
            numfiles_deleted += 1

    if verbose:
        print(f"Deleted {numfiles_deleted} files from the export bucket.")
    pass


def clean_untrusted_bucket(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    date_cutoff_str: str = "7 days ago",
    verbose: bool = True,
):
    """Clean out old files from the "untrusted" bucket in the Secure Ingest pipeline from the client.

    This should be run from the client side.

    Args:
        ivert_config (typing.Union[utils.configfile.Config, None], optional): The IVERT Config to use. Defaults to None.
        date_cutoff_str (str, optional): The date cutoff to use. Defaults to "7 days ago".
        verbose (bool, optional): Whether to print verbose output. Defaults to True.
    """
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    cutoff_datetime = dateparser.parse(date_cutoff_str)

    cutoff_job_number = int(
        f"{cutoff_datetime.year}{cutoff_datetime.month:02d}{cutoff_datetime.day:02d}0000"
    )

    s3m = s3.S3Manager()
    key_list = s3m.listdir(
        ivert_config.s3_import_prefix_base, bucket_type="untrusted", recursive=True
    )

    # Find all job IDs in the keys
    job_id_regex = re.compile(r"(?<=/)\d{12}(?=/)")

    numfiles_deleted = 0
    for key in key_list:
        result = job_id_regex.search(key)
        if result is None:
            continue

        job_id = int(result.group(0))

        if job_id < cutoff_job_number:
            s3m.delete(key, bucket_type="untrusted")
            numfiles_deleted += 1

    if verbose:
        print(f"Deleted {numfiles_deleted} files from the export bucket.")
    pass


def delete_local_jobs_database(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    verbose: bool = True,
):
    """Delete the local jobs database on the client.

    Args:
        ivert_config (typing.Union[utils.configfile.Config, None], optional): The IVERT Config to use. Defaults to None.
        verbose (bool, optional): Whether to print verbose output. Defaults to True.
    """
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    if ivert_config.is_aws:
        raise RuntimeError(
            "'delete_local_jobs_database()' is intended solely for the IVERT client."
        )
    else:
        jobs_db_fname = ivert_config.ivert_jobs_database_local_fname
        if os.path.exists(jobs_db_fname):
            if verbose:
                print(f"Deleting {os.path.basename(jobs_db_fname)}.")
            os.remove(jobs_db_fname)


def delete_local_photon_tiles(
    ivert_config: typing.Union[utils.configfile.Config, None] = None,
    verbose: bool = True,
):
    """Delete files from the local photon_tiles directory on the server, only if there are no active running ivert jobs.

    Args:
        ivert_config (typing.Union[utils.configfile.Config, None], optional): The IVERT Config to use. Defaults to None.
        verbose (bool, optional): Whether to print verbose output. Defaults to True.
    """
    if ivert_config is None:
        ivert_config = utils.configfile.Config()

    if not ivert_config.is_aws:
        raise RuntimeError(
            "'delete_local_photon_tiles()' is intended solely for the IVERT server."
        )

    if not os.path.exists(ivert_config.icesat2_photon_tiles_directory):
        return

    # If there are active running jobs, don't delete anything.
    if ivert_jobs.are_any_ivert_jobs_running():
        raise RuntimeError(
            "There are active running IVERT jobs. Won't delete photon tiles."
        )

    tilenames = [
        fn
        for fn in os.listdir(ivert_config.icesat2_photon_tiles_directory)
        if fn.startswith("photon_tile_")
    ]
    if len(tilenames) == 0:
        return
    elif verbose:
        print(
            f"Deleting {len(tilenames)} files from {ivert_config.icesat2_photon_tiles_directory}."
        )

    rm_cmd = f"rm -rf {ivert_config.icesat2_photon_tiles_directory}/photon_tile_*"
    if verbose:
        print(rm_cmd)
    os.system(rm_cmd)

    return


def define_and_parse_args(return_parser: bool = False):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--what",
        default="all",
        choices=["all", "cache", "jobs", "database", "export", "tiles", "untrusted"],
        help="What to clean from the server. 'all' means all of them. Other choices are 'cache' "
        "(clear the .cudem_cache directory), 'jobs' (clear any local jobs directories), 'database' "
        "(truncate the server's jobs database to only reflect recent jobs, or delete the database "
        "on the client), 'export' (clear the export bucket directories), 'tiles' (delete all"
        "locally-downloaded photon-tiles from the server), and 'untrusted' (clear the 'untrusted' "
        "bucket of old files from the client). Default: 'all'",
    )
    parser.add_argument(
        "--when",
        default="7 days ago",
        help="The date cutoff for cleaning. All records befor that date will be archived, records "
        "on/after that day will be preserved.Can be any string that can be passed to "
        "dateparser.parse(). Default: '7 days ago'",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()
    iconfig = utils.configfile.Config()

    args.what = args.what.lower().strip()

    # If we're on the server, these are our options.
    if iconfig.is_aws:
        if args.what == "all":
            clean_cudem_cache(ivert_config=iconfig)
            clean_old_jobs_dirs(ivert_config=iconfig)
            fix_database_of_orphaned_jobs()
            clean_export_dirs(ivert_config=iconfig, date_cutoff_str=args.when)
            delete_local_photon_tiles(ivert_config=iconfig)
            truncate_jobs_database(date_cutoff_str=args.when)

        elif args.what == "cache":
            clean_cudem_cache(ivert_config=iconfig)

        elif args.what == "jobs":
            clean_old_jobs_dirs(ivert_config=iconfig)

        elif args.what == "database":
            fix_database_of_orphaned_jobs()
            truncate_jobs_database(date_cutoff_str=args.when)

        elif args.what == "export":
            clean_export_dirs(ivert_config=iconfig, date_cutoff_str=args.when)

        elif args.what == "tiles":
            delete_local_photon_tiles(ivert_config=iconfig)

        else:
            print(f"Argument '{args.what}' not implemented for the IVERT server.")

    # not iconfig.is_aws
    else:
        if args.what == "all":
            clean_old_jobs_dirs(ivert_config=iconfig)
            clean_untrusted_bucket(ivert_config=iconfig, date_cutoff_str=args.when)
            delete_local_jobs_database(ivert_config=iconfig)

        elif args.what == "jobs":
            clean_old_jobs_dirs(ivert_config=iconfig)

        elif args.what == "database":
            delete_local_jobs_database(ivert_config=iconfig)

        elif args.what == "untrusted":
            clean_untrusted_bucket(ivert_config=iconfig, date_cutoff_str=args.when)

        elif args.what == "export":
            clean_export_dirs(ivert_config=iconfig, date_cutoff_str=args.when)

        else:
            print(f"Argument '{args.what}' not implemented for the IVERT client.")
