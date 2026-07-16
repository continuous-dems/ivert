import os
import re

import s3
import jobs_database
import typing

username_regex = re.compile(r"(?<=^)[a-zA-Z0-9\.]+(?=_\d{12}$)")
job_id_regex = re.compile(r"(?<=_)\d{12}(?=$)")


def get_username(job_name: str) -> str:
    try:
        return username_regex.search(job_name).group(0)
    except AttributeError:
        raise ValueError(f"{job_name} is not a valid IVERT job name.")


def get_job_id(job_name: str) -> int:
    try:
        return int(job_id_regex.search(job_name).group(0))
    except AttributeError:
        raise ValueError(f"{job_name} is not a valid IVERT job name.")


class IvertExporter:
    """Class for exporting files from the IVERT server to the EXPORT bucket to go to the client.

    This is it its own separate class & module because it's used by several other modules in the code hierarchy.
    """

    def __init__(
        self,
        s3_manager: typing.Union[s3.S3Manager, None] = None,
        jobs_db: typing.Union[jobs_database.JobsDatabaseServer, None] = None,
    ):
        if s3_manager:
            self.s3m = s3_manager
        else:
            self.s3m = s3.S3Manager()

        if jobs_db:
            self.jobs_db = jobs_db
        else:
            self.jobs_db = jobs_database.JobsDatabaseServer()

    def upload_file_to_export_bucket(
        self,
        job_name_or_id: typing.Union[str, int],
        fname: str,
        username: typing.Union[str, None] = None,
        overwrite: bool = False,
        upload_to_s3: bool = True,
    ) -> None:
        """When exporting a file, upload it here and update the file's status in the database.

        Args:
            job_name_or_id (str or int): The name (string) or id (int) of the job to export.
            fname (str): The path to the file to export.
            username (str, optional): The username of the user who exported the file, if an integer job_id is given in
                the first argument. Otherwise unused. Defaults to None.
            overwrite (bool, optional): Whether to overwrite the file if it already exists. Defaults to False.
            upload_to_s3 (bool, optional): Whether to upload the updated database to the export bucket. Defaults to True.

        If the file doesn't yet exist in the databse, add an entry to the jobs_database for this exported file."""
        if not os.path.exists(fname):
            return

        if type(job_name_or_id) is int or str.isnumeric(job_name_or_id):
            username = username
            if username is None:
                raise ValueError(
                    "username must be specified if job_name_or_id is an integer."
                )
            job_id = int(job_name_or_id)
        else:
            job_id = get_job_id(job_name_or_id)
            username = get_username(job_name_or_id)

        export_prefix = self.jobs_db.populate_export_prefix_if_not_set(
            username, job_id, increment_vnum=False, upload_to_s3=False
        )

        f_key = (
            export_prefix
            + ("" if export_prefix.endswith("/") else "/")
            + os.path.basename(fname)
        )

        # Determine which export bucket we're using.
        if self.s3m.config.use_export_alt_bucket:
            bucket_type = "export_alt"
        else:
            bucket_type = "export_server"

        # Upload the file, only if it doesn't already exist in the export bucket.
        if overwrite or not self.s3m.exists(
            f_key, bucket_type=bucket_type, return_head=False
        ):
            self.s3m.upload(fname, f_key, bucket_type=bucket_type)

        # Add an export file entry into the database for this job.
        if self.jobs_db.file_exists(username, job_id, fname):
            self.jobs_db.update_file_statistics(
                username,
                job_id,
                fname,
                new_status="uploaded",
                upload_to_s3=upload_to_s3,
            )
        else:
            self.jobs_db.create_new_file_record(
                fname, job_id, username, 1, status="uploaded", upload_to_s3=upload_to_s3
            )

        return
