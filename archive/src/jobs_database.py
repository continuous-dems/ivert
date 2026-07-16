#!/usr/bin/env python
## A module for managing the IVERT jobs database.

import argparse
import botocore.exceptions
import dateparser

# import datetime
import os
import pandas
import re
import sqlite3
import sys
import time
import typing

if vars(sys.modules[__name__])["__package__"] in ("ivert", "ivert_utils"):
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.configfile as configfile
    import ivert_utils.version_check_server as version_check_server
    import ivert_utils.version as version
    import ivert.s3 as s3
else:
    try:
        import utils.configfile as configfile
        import utils.version as version
        import utils.version_check_server as version_check_server
        import s3
    except ModuleNotFoundError:
        # It's weird, when running jobs_database.py as a standalone script in the IVERT package, it fails to import
        # these modules above, but this works.
        import ivert_utils.configfile as configfile
        import ivert_utils.version_check_server as version_check_server
        import ivert_utils.version as version
        import ivert.s3 as s3

ivert_config = None


class JobsDatabaseClient:
    """Base class for common operations on the IVERT jobs database.

    Consists of methods used both by the EC2-server and the IVERT client.

    Functionality specific to the EC2-server is implemented in the IvertJobsDatabaseServer class.
    """

    def __init__(self):
        """
        Initializes a new instance of the IvertJobsDatabase_BaseClass class.

        Args:

        Returns:
            None
        """
        # The IVERT configfile object. Get the paths from here.
        global ivert_config
        if ivert_config is None:
            ivert_config = configfile.Config()

        self.ivert_config = ivert_config
        self.ivert_jobs_dir = self.ivert_config.ivert_jobs_directory_local
        self.db_fname = self.ivert_config.ivert_jobs_database_local_fname

        # The schema file.
        self.schema_file = self.ivert_config.ivert_jobs_db_schema_file

        # The database connection
        self.conn = None

        # Where the jobs database sits in the S3 bucket
        if ivert_config.use_export_alt_bucket:
            self.s3_bucket_type = (
                self.ivert_config.s3_ivert_jobs_database_bucket_type_export_alt
            )
            self.s3_database_key = (
                self.ivert_config.s3_ivert_jobs_database_alt_client_key
            )
        else:
            if ivert_config.is_aws:
                self.s3_bucket_type = (
                    self.ivert_config.s3_ivert_jobs_database_bucket_type_server
                )
                self.s3_database_key = (
                    self.ivert_config.s3_ivert_jobs_database_server_key
                )
            else:
                self.s3_bucket_type = (
                    self.ivert_config.s3_ivert_jobs_database_bucket_type_client
                )
                self.s3_database_key = (
                    self.ivert_config.s3_ivert_jobs_database_client_key
                )

        # The S3Manager instance, for uploading and downloading files to the S3 buckets
        self.s3m = s3.S3Manager()

        # The metadata key for the latest job in the database, to tag with the file in the s3 bucket.
        self.s3_latest_job_metadata_key = (
            self.ivert_config.s3_jobs_db_latest_job_metadata_key
        )
        self.s3_vnum_metadata_key = (
            self.ivert_config.s3_jobs_db_version_number_metadata_key
        )
        self.s3_jobs_since_metadata_key = (
            self.ivert_config.s3_jobs_db_jobs_since_metadata_key
        )

        return

    def download_from_s3(self, only_if_newer: bool = True) -> None:
        """
        Fetches the IVERT jobs database from the S3 bucket.

        Args:
            only_if_newer (bool, optional): Whether to only download if the local copy is older than the one in the S3
                bucket. Defaults to Truej. If the s3 version is newer, the local copy will be deleted before downloading
                the new verison.
            overwrite (bool, optional): Whether to overwrite the local copy if it already exists. Defaults to False.

        Returns:
            None

        Raises:
            FileNotFoundError: If the specified bucket type in S3 doesn't exist.
        """
        # Ensure that the S3Manager instance is valid
        assert isinstance(self.s3m, s3.S3Manager)

        # Get the S3 key and local database file name
        db_key = self.s3_database_key
        local_db = self.db_fname
        db_btype = self.s3_bucket_type

        # Check if the database exists in the S3 bucket
        if not self.s3m.exists(db_key, bucket_type=db_btype):
            raise FileNotFoundError(
                f"The {db_key} database doesn't exist in the S3 bucket."
            )

        if only_if_newer and os.path.exists(local_db):
            if not self.is_s3_newer_than_local():
                return

            # If the s3 version number is greater than the local version number, delete the local version.
            elif os.path.exists(local_db):
                if self.conn:
                    self.conn.close()
                    self.conn = None
                os.remove(local_db)

        # Close any open connections to the existing database.
        if self.conn:
            self.conn.close()
            self.conn = None

        # Download the database from the S3 bucket. Use a tempfile to help minimize race conditions with different
        # processes querying the database and deleting it while another is trying to read it.
        self.s3m.download(db_key, local_db, bucket_type=db_btype, use_tempfile=True)

        return

    def get_connection(self) -> sqlite3.Connection:
        """Returns the database connection for the specified database type, and intializes it if necessary.

        Returns:
            sqlite3.Connection: The database connection.
        """
        # If the connection isn't open yet, open it.
        if self.conn is None:
            # Check if the database exists
            # Sometimes if the database is being re-downloaded by another process, it might suddenly not exist. In
            # that case, we'll pause juse a moment and then try again.
            num_tries = 0
            max_num_tries = 5
            wait_time_s = 0.05

            while not self.exists(local_or_s3="local") and num_tries < max_num_tries:
                num_tries += 1
                time.sleep(wait_time_s)

            if not self.exists(local_or_s3="local"):
                raise FileNotFoundError(
                    f"IVERT jobs database doesn't exist in '{self.db_fname}'."
                )

            # Open the database
            conn = sqlite3.connect(self.db_fname)
            # We're setting row_factory to sqlite3.Row to return key-indexed dictionaries instead of tuples
            conn.row_factory = sqlite3.Row
            # Enforce foreign key constraints whenever we open the database
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.commit()
            self.conn = conn

        else:
            # Test to make sure the database is valid and still open. This can be false if the database was reset.
            test_query = "SELECT name FROM sqlite_master WHERE type='table' AND name='ivert_jobs' LIMIT 1;"
            try:
                self.conn.execute(test_query)
            except sqlite3.ProgrammingError:
                # If the database was reset, close the connection and reset it
                self.conn.close()
                self.conn = None
                return self.get_connection()

        # Return the connection
        return self.conn

    def is_s3_newer_than_local(self):
        """Return True if the jobs_database on the S3 bucket is newer than the local copy. False otherwise."""
        s3_vnum = self.fetch_latest_db_vnum_from_s3_metadata()
        local_vnum = self.fetch_latest_db_vnum_from_database()
        s3_jobs_since = self.earliest_job_number("s3")
        local_jobs_since = self.earliest_job_number("database")

        return (s3_vnum > local_vnum) or (s3_jobs_since > local_jobs_since)

    def make_pickleable(self):
        """Makes the database connection pickleable.

        Returns:
            None
        """
        if self.conn:
            self.conn.close()

        del self.conn
        self.conn = None

    def exists(self, local_or_s3: str = "local") -> bool:
        """Checks if the database exists.

        Args:
            local_or_s3 (str): Whether to check if the "local" or "s3" database exists.

        Returns:
            bool: True if the database exists, False otherwise.
        """
        local_or_s3 = local_or_s3.strip().lower()

        if local_or_s3 == "local":
            return os.path.exists(self.db_fname)
        elif local_or_s3 == "s3":
            return self.s3m.exists(
                self.s3_database_key, bucket_type=self.s3_bucket_type
            )
        else:
            raise ValueError(
                f'local_or_s3 must be "local" or "s3". "{local_or_s3}" not recognized.'
            )

    def __del__(self):
        """
        Commit any pending transactions and close the database connections.

        Returns:
            None
        """
        if self.conn:
            self.conn.commit()
            self.conn.close()

    def fetch_latest_job_number_from_database(self) -> int:
        """Fetch the last job number from the database, in YYYYMMDDNNNN format.

        This is for querying the database itself, not the metdata in the S3 bucket.

        Returns:
            int: The last job number, in YYYYMMDDNNNN format.
        """
        conn = self.get_connection()
        cur = conn.cursor()
        results = cur.execute("SELECT MAX(job_id) FROM ivert_jobs;").fetchall()

        # The results is a list of sqlite.Row objects. Should be only one long of my logic is right here
        assert len(results) == 1

        resultnum = results[0]["MAX(job_id)"]

        # If the result is None, it means there are no jobs in the database yet. Just return 0.
        # Else return the highest job number
        if resultnum is None:
            return 0
        else:
            return resultnum

    def fetch_latest_job_number_from_s3_metadata(self) -> typing.Union[int, None]:
        """Fetch the last job number from the S3 metadata.

        This is for querying the metadata in the S3 bucket, not the database itself.

        Returns:
            int: The last job number, in YYYYMMDDNNNN format. 0 if there are not jobs or the databasew doesn't exist in
                    the S3 bucket.

            or...

            None: If the database doesn't exist in the S3 bucket or the 'latest_job' metadata key is missing.
        """
        # If the result is None, it means there are no jobs in the database yet or it wasn't updated with the
        # latest_job number in the S3 metadata. Just return 0.
        # Else return the highest job number.
        if self.s3m.exists(self.s3_database_key, bucket_type=self.s3_bucket_type):
            md = self.s3m.get_metadata(
                self.s3_database_key, bucket_type=self.s3_bucket_type
            )
            if md is not None:
                if self.s3_latest_job_metadata_key in md.keys():
                    return int(md[self.s3_latest_job_metadata_key])
                elif (
                    "latest_job" in md.keys()
                ):  # For now, maintain backward compatibility with old metadata keys.
                    return int(md["latest_job"])
                elif "ivert_latest_job" in md.keys():
                    return int(md["ivert_latest_job"])
            else:
                return None
        # If the database doesn't exist in the S3 bucket, just return 0.
        else:
            return None

    def fetch_latest_db_vnum_from_database(self) -> int:
        """Fetch the latest version number from the local database.

        Returns:
            int: The last version number from the 'vnum' entry in the database.
        """
        conn = self.get_connection()
        return (
            conn.cursor()
            .execute("SELECT vnum FROM vnumber LIMIT 1;")
            .fetchall()[0]["vnum"]
        )

    def fetch_latest_db_vnum_from_s3_metadata(self) -> typing.Union[int, None]:
        """Fetch the last version number from the S3 metadata.

        This is for querying the metadata in the S3 bucket, not the database itself.

        Returns:
            int: The last version number from the 'vnum' metadata key.

            or...

            None: If the database doesn't exist in the S3 bucket or the 'vnum' metadata key doesn't exist.
        """
        if self.s3m.exists(self.s3_database_key, bucket_type=self.s3_bucket_type):
            md = self.s3m.get_metadata(
                self.s3_database_key, bucket_type=self.s3_bucket_type
            )
            if md is not None:
                if self.s3_vnum_metadata_key in md.keys():
                    return int(md[self.s3_vnum_metadata_key])
                elif (
                    "vnum" in md.keys()
                ):  # For now, maintain backward compatibility with old metadata keys.
                    return int(md["vnum"])
                elif "ivert_db_vnum" in md.keys():
                    return int(md["ivert_db_vnum"])
                else:
                    return None
            else:
                return None
        # If the database doesn't exist in the S3 bucket, just return None.
        else:
            return None

    def fetch_ivert_version_from_s3_metadata(self) -> typing.Union[str, None]:
        """Fetch the ivert software version currently running on the EC2 from the S3 metadata.

        Returns:
            str: The ivert software version currently running on the EC2.
            or...
            None: If the 'ivert_version' metadata key doesn't exist in the S3 metadata."""
        if self.s3m.exists(self.s3_database_key, bucket_type=self.s3_bucket_type):
            md = self.s3m.get_metadata(
                self.s3_database_key, bucket_type=self.s3_bucket_type
            )
            if md is not None:
                if self.ivert_config.s3_jobs_db_ivert_version_metadata_key in md.keys():
                    return md[self.ivert_config.s3_jobs_db_ivert_version_metadata_key]
                elif (
                    "ivert_version" in md.keys()
                ):  # For now, maintain backward compatibility with old metadata keys.
                    return md["ivert_version"]
            else:
                return None
        # If the database doesn't exist in the S3 bucket, just return None.
        else:
            return None

    def fetch_earliest_job_number_from_s3_metadata(self) -> int:
        """Fetch the earliest job number from the local database.

        Returns:
            int: The earliest job number from the 'job_id' entry in the database.
        """
        if self.s3m.exists(self.s3_database_key, bucket_type=self.s3_bucket_type):
            md = self.s3m.get_metadata(
                self.s3_database_key, bucket_type=self.s3_bucket_type
            )
            if md is not None and self.s3_jobs_since_metadata_key in md.keys():
                if self.s3_jobs_since_metadata_key in md:
                    return int(md[self.s3_jobs_since_metadata_key])
                elif (
                    "jobs_since" in md
                ):  # For now, maintain backward compatibility with old metadata keys.
                    return int(md["jobs_since"])
                elif "ivert_jobs_since" in md:
                    return int(md["ivert_jobs_since"])
            else:
                return 0
        # If the database doesn't exist in the S3 bucket, just return None.
        else:
            return 0

    def fetch_earliest_job_number_from_database(self) -> int:
        """Fetch the earliest job number from the local database.

        Returns:
            int: The earliest job number from the 'job_id' entry in the database.
        """
        conn = self.get_connection()
        resp = conn.cursor().execute("SELECT MIN(job_id) FROM ivert_jobs;").fetchall()

        if len(resp) == 0:
            return 0
        else:
            resp_item = resp[0]["MIN(job_id)"]
            if resp_item is None:
                return 0
            else:
                return int(resp_item)

    def earliest_job_number(self, source_data: str = "database"):
        """Get the earliest job number from the database.

        Fetch from either the local database ('database') or from the S3 metadata ('s3')."""
        src_to_use = source_data.lower().strip()
        if src_to_use == "database":
            return self.fetch_earliest_job_number_from_database()
        elif src_to_use == "s3":
            return int(self.fetch_earliest_job_number_from_s3_metadata())
        else:
            raise ValueError(
                f"Unrecognized source_data: {source_data}. Must be 'database' or 's3'."
            )

    def job_exists(
        self, username, job_id, return_row: bool = False
    ) -> typing.Union[bool, sqlite3.Row]:
        """Returns True/False whether a (username, job_id) job presently exists in the database or not.

        If return_row is True, then it returns the sqlite3.Row object for the job if it exists.
        """
        conn = self.get_connection()
        cur = conn.cursor()
        if return_row:
            results = cur.execute(
                "SELECT * FROM ivert_jobs WHERE username = ? AND job_id = ? LIMIT 1;",
                (username, job_id),
            ).fetchone()
            if results is None:
                return False
            else:
                return results
        else:
            cur.execute(
                "SELECT count(*) FROM ivert_jobs WHERE username = ? AND job_id = ?;",
                (username, job_id),
            )
            count = cur.fetchone()[0]
            if count == 0:
                return False
            else:
                assert count == 1
                return True

    def file_exists(
        self, filename, username, job_id, return_row: bool = False
    ) -> typing.Union[bool, sqlite3.Row]:
        """Returns True/False whether the (filename, username, job_id) file already exists in the ivert_files table or not.

        If return_row is True, then it returns to the sqlite3.Row object for the file record if it exists."""
        conn = self.get_connection()
        cur = conn.cursor()
        if return_row:
            results = cur.execute(
                "SELECT * FROM ivert_files WHERE filename = ? AND username = ? AND job_id = ?;",
                (filename, username, job_id),
            )

            if results is None:
                return False
            else:
                return results.fetchone()

        else:
            cur.execute(
                "SELECT count(*) FROM ivert_files WHERE filename = ? AND username = ? AND job_id = ?;",
                (filename, username, job_id),
            )
            count = cur.fetchone()[0]
            if count == 0:
                return False
            else:
                assert count == 1
                return True

    def get_params_from_s3_path(self, s3_key, bucket_type=None) -> dict:
        """Get the parameters from the S3 path.

        IVERT input-output paths are of the form "s3://[BUCKET_NAME]/[common prefix]/[command]/[username]/[YYYYMMDDNNNN]/[filename]

        Return a dictionary with the "command", "username", and "job_id" parameters.
        """
        if not bucket_type:
            bucket_type = self.s3_bucket_type
        else:
            bucket_type = bucket_type.strip().lower()

        # Separate string into parts
        s3_parts = [part for part in s3_key.split("/") if len(part) > 0]
        job_id_regex = re.compile(r"^(\d{12})$")
        # The last part should be the job ID. If this is a path to a file, remove the file part at the end.
        if not job_id_regex.match(s3_parts[-1]):
            s3_parts.pop()

        # The last part should be the job_id
        assert job_id_regex.match(s3_parts[-1])

        # 2nd-to-last part should be the username
        assert re.match("^[a-zA-Z0-9_.-]+$", s3_parts[-2])

        # 3rd-to-last part should be command
        assert s3_parts[-3] in self.ivert_config.ivert_commands

        return {
            "command": s3_parts[-3],
            "username": s3_parts[-2],
            "job_id": s3_parts[-1],
        }

    def get_job_path_from_params(
        self, command, username, job_id, local_os: bool = False
    ) -> str:
        """Get the relative folder path from the parameters, outlined in ivert_config::s3_ivert_job_subdirs_template

        Args:
            command (str): The command.
            username (str): The username.
            job_id (int or str): The job ID.
            local_os (bool): Whether the path is on the local OS. Defaults to False. Only matters if we're on a Windows
                             machine with a different path separator.

        Returns:
            str: The relative folder path."""
        job_path_template = self.ivert_config.s3_ivert_job_subdirs_template
        path = (
            job_path_template.replace("[command]", command.strip().lower())
            .replace("[username]", username.strip().lower())
            .replace("[job_id]", str(job_id).strip().lower())
        )

        if local_os:
            path = path.replace("/", os.sep)
        return path

    def delete_database(self) -> None:
        """Deletes the database locally.

        Does not touch the database in the S3 bucket.

        Returns:
            None
        """
        if self.conn:
            self.conn.close()
        os.remove(self.db_fname)

    def is_valid(self) -> bool:
        """Return True if the database is internally valid."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute("PRAGMA integrity_check;")
        resp = cursor.fetchone()[0]
        if resp.lower() == "ok":
            return True
        else:
            return False

    def len(self, table_name: str) -> int:
        """Return how many rows exist in the given table.

        Some table shortnames are accepted such as "jobs", "files", "subscritions" or "messages".
        """
        table_name = table_name.strip().lower()
        if table_name == "jobs":
            table_name = "ivert_jobs"
        elif table_name == "files":
            table_name = "ivert_files"
        elif table_name == "subscriptions":
            table_name = "sns_subscriptions"
        elif table_name == "messages":
            table_name = "sns_messages"

        conn = self.get_connection()
        cursor = conn.cursor()
        count = cursor.execute(f"SELECT COUNT(*) FROM {table_name};").fetchone()[0]
        return count

    def read(
        self,
        table_name: str,
        username: typing.Union[str, None] = None,
        job_id: typing.Union[str, None] = None,
    ) -> pandas.DataFrame:
        """Shorthand for read_table_as_pandas_df."""
        return self.read_table_as_pandas_df(table_name, username, job_id)

    def read_table_as_pandas_df(
        self,
        table_name: str,
        username: typing.Union[str, None] = None,
        job_id: typing.Union[str, int, None] = None,
    ) -> pandas.DataFrame:
        """Read a table and return as a pandas dataframe.

        Args:
            table_name (str): The name of the table to read. Can also be a shortname such as "jobs", "files", "subscriptions" or "messages".
            username (str): The username to filter on. Defaults to None, which means no filter.
            job_id (str, int): The job ID to filter on. Defaults to None, which means no filter.

        Returns:
            A pandas dataframe of the table in question.
        """
        table_name = table_name.strip().lower()
        if table_name == "jobs":
            table_name = "ivert_jobs"
        elif table_name == "files":
            table_name = "ivert_files"
        elif table_name in ("subscriptions", "subs"):
            table_name = "sns_subscriptions"
        elif table_name == "messages":
            table_name = "sns_messages"
        elif table_name == "vnum":
            table_name = "vnumber"

        # Build the query
        query = f"SELECT * FROM {table_name}"
        if username is not None:
            query += f" WHERE username = '{username}'"
        if job_id is not None:
            if username is None:
                query += f" WHERE job_id = '{job_id}'"
            else:
                query += f" AND job_id = '{job_id}'"
        query += ";"

        conn = self.get_connection()
        return pandas.read_sql_query(query, conn)

    def get_sns_arn(self, email: str) -> str:
        """Get the SNS ARN for the given email address.

        This assumes there is only on ARN per email address. If there are multiple ARNs, this will return the first one.

        Args:
            email (str): The email address to get the ARN for.

        Returns:
            The SNS ARN for the given email address.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT sns_arn FROM sns_subscriptions WHERE user_email = '{email}';"
        )
        return cursor.fetchone()[0]

    def get_job_from_pid(self, pid: int) -> sqlite3.Row:
        """Get the job from the pid.

        Args:
            pid (int): The pid of the job.

        Returns:
            The ivert_jobs row for the job from the pid."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM ivert_jobs WHERE pid = {pid};")
        return cursor.fetchone()

    def job_status(self, username: str, job_id: int) -> typing.Union[str, None]:
        """Fetch the job status from the database.

        Args:
            username (str): The username associated with the job.
            job_id (int): The job_id associated with the job.

        Returns:
            str, or None: The status from the ivert_jobs table. None if no job of that id exists.
        """
        query = """SELECT status FROM ivert_jobs
                WHERE username = ?
                AND job_id = ?"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(query, (username, job_id))
        result = cursor.fetchone()
        if result:
            return result[0]
        else:
            return None

    def list_unfinished_jobs(
        self, return_rows: bool = False
    ) -> typing.Union[list[tuple], list[sqlite3.Row]]:
        """Return a list of all jobs whose status is marked as 'started', 'running' or 'unknown'.

        NOTE: This does not denote whether the job is actually still running or not.

        If return_rows, return a list of sqlite3.Row objects.
        Else, return a list of tuples of (username, job_id, job_pid, status).
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""SELECT username, job_id, job_pid, status
                          FROM ivert_jobs WHERE status IN ('started', 'running', 'unknown');""")
        rows = cursor.fetchall()
        if return_rows:
            return rows
        else:
            return [
                {
                    "username": row["username"],
                    "job_id": row["job_id"],
                    "job_pid": row["job_pid"],
                    "status": row["status"],
                }
                for row in rows
            ]


class JobsDatabaseServer(JobsDatabaseClient):
    """Class for managing the IVERT jobs database on the EC2 (server).

    The base class can only read and query the database. The server class can write to the database and upload it."""

    def __init__(self):
        """
        Initializes a new instance of the IvertJobsDatabaseServer class.

        Args:

        Returns:
            None
        """
        super().__init__()

    def create_new_database(
        self,
        database_fname: typing.Union[str, None] = None,
        only_if_not_exists_in_s3: bool = True,
        overwrite: bool = False,
        verbose: bool = True,
    ) -> typing.Union[sqlite3.Connection, None]:
        """
        Creates a new database from scratch.

        Args:
            database_fname (str): The name of the database file to create. Defaults to self.db_fname, but can be
                                  different if creating a different/temp database.
            only_if_not_exists_in_s3 (bool): Whether to warn if the database already exists in the S3 bucket.
            overwrite (bool): Whether to overwrite the database if it already exists.
            verbose (bool): Whether to print verbose output.

        Returns:
            A sqlite connection to the newly created database.
        """
        # Make sure the schema text file exists and is a complete and valid SQL statement
        assert os.path.exists(self.schema_file)
        schema_text = open(self.schema_file, "r").read()
        assert sqlite3.complete_statement(schema_text)

        # Create database in its location outlined in ivert_config.ini (read by the base-class constructor)
        if not database_fname:
            database_fname = self.db_fname

        if only_if_not_exists_in_s3 and self.s3m.exists(
            self.s3_database_key, bucket_type=self.s3_bucket_type
        ):
            raise RuntimeError(
                "The jobs database already exists in the S3 bucket. Delete from there first:\n"
                "> python s3.py rm {} -b {}".format(
                    self.s3_database_key, self.s3_bucket_type
                )
            )

        if os.path.exists(database_fname):
            if overwrite:
                os.remove(database_fname)
                if verbose:
                    print(f"Delete existing {database_fname}")
            elif database_fname == self.db_fname:
                return self.get_connection()
            else:
                conn = sqlite3.connect(self.db_fname)
                # We're setting row_factory to sqlite3.Row to return key-indexed dictionaries instead of tuples
                conn.row_factory = sqlite3.Row
                return conn

        # Create the database
        conn = sqlite3.connect(database_fname)
        conn.row_factory = sqlite3.Row
        conn.executescript(schema_text)
        conn.execute("PRAGMA foreign_keys = ON;")

        conn.commit()

        # Assign it to the object attribute
        if database_fname == self.db_fname:
            self.conn = conn

        # Return the connection
        return conn

    def update_job_status(
        self,
        job_username: str,
        job_id: typing.Union[int, str],
        status: str,
        new_pid: typing.Union[int, None] = None,
        increment_vnum: bool = True,
        upload_to_s3: bool = True,
    ):
        """
        Updates a job record in the existing database.

        Args:
            job_username (str): The username of the job.
            job_id (int or str): The ID of the job.
            status (str): The new status of the job.
            new_pid (int): The new process ID of the job, to replace the existing process_pid in the database
                           (this is useful if the job entry was originally added as a placeholder).
            increment_vnum (bool): Whether to increment the database version number. This may be set to false if
                                      several changes are being made at once, including this one.
                                      If this is set to False, then the database version number will not be incremented
                                      and the change will not be committed.
            upload_to_s3 (bool): Whether to upload the database to the S3 bucket.

        Returns:
            None

        Raises:
            ValueError: If the job does not exist in the database.
        """
        # Convert job_id to int if it's a string
        job_id = int(job_id)

        # First check to see if the job exists
        results = self.job_exists(job_username, job_id, return_row=True)
        if results:
            # Check to see if the status is different, or if the PID is different. If not, don't do anything.
            if results["status"] == status and (
                new_pid is None or results["job_pid"] == new_pid
            ):
                return
        else:
            raise ValueError(
                f"Job {job_username}_{job_id} does not exist in the database."
            )

        # Connect to the database
        conn = self.get_connection()
        cursor = conn.cursor()

        # Build the update statement
        update_stmt = """UPDATE ivert_jobs
                         SET status = ?"""
        if new_pid is not None:
            update_stmt += ", job_pid = ?"
        update_stmt += """
                         WHERE username = ? AND job_id = ?;"""

        if new_pid is not None:
            cursor.execute(update_stmt, (status, new_pid, job_username, job_id))
        else:
            cursor.execute(update_stmt, (status, job_username, job_id))

        # Increment the database version number
        if increment_vnum:
            self.increment_vnumber(cursor)

        conn.commit()

        # Upload the database to the S3 bucket
        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        return

    def update_file_status(
        self,
        username: str,
        job_id: typing.Union[int, str],
        filename: str,
        status: str,
        new_size: typing.Union[int, None] = None,
        increment_vnum: bool = True,
        upload_to_s3: bool = True,
    ) -> None:
        """
        Updates a file record in the existing database.

        Args:
            username (str): The username of the job.
            job_id (int or str): The ID of the job.
            filename (str): The name of the file.
            status (str): The new status of the file. Look in ivert_jobs_schema.sql::ivert_files::status for the possible statuses.
            increment_vnum (bool): Whether to increment the database version number. This may be set to false if we're making other sequential changes.
            upload_to_s3 (bool): Whether to upload the database to the S3 bucket. Default True.

        Returns:
            None

        Raises:
            ValueError: If the file does not exist in the database.
        """
        # Convert job_id to str if it's an int
        job_id = str(job_id)
        # Use only the basename of the filename. Paths are stored in the ivert_jobs table.
        file_basename = os.path.basename(filename)

        # First check to see if the file exists
        result = self.file_exists(file_basename, username, job_id, return_row=True)
        if result:
            # Check to see if the status is different. If not, don't do anything.
            if result["status"] == status:
                return
        else:
            raise ValueError(f"File {file_basename} does not exist in the database.")

        conn = self.get_connection()
        cursor = conn.cursor()

        update_stmt = """UPDATE ivert_files
                         SET status = ?"""

        if new_size is not None:
            update_stmt += ", size_bytes = ?"

        update_stmt += """
                         WHERE username = ? AND job_id = ? AND filename = ?;"""

        if new_size is None:
            cursor.execute(update_stmt, (status, username, job_id, file_basename))
        else:
            cursor.execute(
                update_stmt, (status, new_size, username, job_id, file_basename)
            )

        # Increment the database version number
        if increment_vnum:
            self.increment_vnumber(cursor)

        # Commit the changes
        conn.commit()

        # Upload the database to the S3 bucket
        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        return

    def update_file_statistics(
        self,
        username: str,
        job_id: typing.Union[int, str],
        filename: str,
        new_status: typing.Union[str, None] = None,
        increment_vnumber: bool = True,
        upload_to_s3: bool = True,
    ) -> None:
        """
        Updates a file record in the existing database with new md5 and file size statistics.

        A file record can be created and then the file changed (such as a job logfile, for instance).
        If that happens, use this method to update the statistics in the table.

        Args:
            username (str): The username of the job.
            job_id (int or str): The ID of the job.
            filename (str): The name of the file.
            new_status (str): A new status for the job. Default: do not change the status.
            increment_vnumber (bool): Whether to increment the database version number. This may be set to false if we're making other sequential changes.
            upload_to_s3 (bool): Whether to upload the database to the S3 bucket. Default True.

        Returns:
            None

        Raises:
            ValueError: If the file does not exist in the database.
        """
        # Convert job_id to str if it's an int
        job_id = str(job_id)
        file_basename = os.path.basename(filename)

        if not os.path.exists(filename):
            raise ValueError(f"File {filename} does not exist.")

        # Compute the stats
        md5 = self.s3m.compute_md5(filename)
        size_bytes = os.path.getsize(filename)

        conn = self.get_connection()
        cursor = conn.cursor()

        if new_status:
            update_query = """UPDATE ivert_files
                              SET md5 = ?, size_bytes = ?, status = ?
                              WHERE username = ? AND job_id = ? AND filename = ?;"""

            cursor.execute(
                update_query,
                (md5, size_bytes, new_status, username, job_id, file_basename),
            )

        else:
            update_query = """UPDATE ivert_files
                              SET md5 = ?, size_bytes = ?
                              WHERE username = ? AND job_id = ? AND filename = ?;"""

            cursor.execute(
                update_query, (md5, size_bytes, username, job_id, file_basename)
            )

        # Increment the database version number
        if increment_vnumber:
            self.increment_vnumber(cursor)

        # Commit the changes
        conn.commit()

        # Upload the database to the S3 bucket
        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        return

    def upload_to_s3(self, only_if_newer: bool = True) -> None:
        """
        Uploads the database to the S3 bucket to be externally accessible.

        Also adds the latest_job number to the S3 metadata.

        Args:
            only_if_newer (bool): Whether to only upload the database if it's newer than the one already on S3. This
                can be used to avoid uploading the same database multiple times. It looks at the vnum parameter in the s3 metadata.

        Returns:
            None
        """
        # If we don't have the most recent version saved locally, and 'only_if_newer' is set, then don't upload.
        if only_if_newer and self.is_s3_newer_than_local():
            return

        latest_job_id = self.fetch_latest_job_number_from_database()
        local_filename = self.db_fname
        db_s3_key = self.s3_database_key
        database_earliest_job_id = self.fetch_earliest_job_number_from_database()

        # Upload the database to the S3 bucket.
        # Set the md5, the latest_job number, and the vnum in the S3 metadata.
        # Also set the jobs_since key to whatever the first job number is in the database is set to.
        self.s3m.upload(
            local_filename,
            db_s3_key,
            bucket_type=self.s3_bucket_type,
            include_md5=True,
            other_metadata={
                self.s3_latest_job_metadata_key: str(latest_job_id),
                self.s3_vnum_metadata_key: str(
                    self.fetch_latest_db_vnum_from_database()
                ),
                self.ivert_config.s3_jobs_db_ivert_version_metadata_key: version.__version__,
                self.ivert_config.s3_jobs_db_jobs_since_metadata_key: str(
                    database_earliest_job_id
                ),
                self.ivert_config.s3_jobs_db_min_client_version_metadata_key: version_check_server.minimum_client_version(),
            },
        )

        return

    def create_new_job(
        self,
        job_config_obj: configfile.Config,
        job_configfile: str,
        job_logfile: str,
        job_local_dir: str,
        job_local_output_dir: str,
        job_import_prefix: str,
        job_export_prefix: str,
        job_status: str = "unknown",
        update_vnum: bool = True,
        upload_to_s3: bool = True,
    ) -> sqlite3.Row:
        """
        Given the prefix of a job in the S3 bucket, create a new job in the "ivert_jobs" table of the database.

        If the (username, job_id) tuple already exists in the database, the job is not created but the Row is returned.
        No files are created here.

        Args:
            job_config_obj (utils.configfile.Config object): A parsed configuraiton object of the job_config.ini file.
            job_configfile (str): the name of the job configuration file.
            job_logfile (str): the name of the logfile for this job.
            job_local_dir (str): the local directory where this job's files will be downloaded.
            job_import_prefix (str): the prefix of the job's files in the S3 import bucket.
            job_export_prefix (str): the prefix of the job's files in the S3 export bucket.
            job_local_output_dir (str): the local directory where this job's output files will be written.
            job_status (str): The status of the job upon creation in the database. Defaults to "unknown."
            update_vnum (bool): Whether to update the database version number. Defaults to True.
            upload_to_s3 (bool): Whether to upload the database to the S3 bucket. Defaults to True.

        Returns:
            The database row (record) of the new job in the "ivert_jobs" table.
        """
        # job_config_obj should be a configfile.Config object with fields defined in
        # Config/ivert_job_config_TEMPLATE.ini
        jco = job_config_obj

        # Check if the (username, job_id) tuple already exists in the database.
        # If so, just return it.
        if hasattr(jco, "job_id") and hasattr(jco, "username"):
            existing_row = self.job_exists(jco.username, jco.job_id, return_row=True)
        else:
            existing_row = None

        if existing_row:
            return existing_row

        if not (
            hasattr(jco, "job_id")
            and hasattr(jco, "username")
            and hasattr(jco, "ivert_command")
        ):
            # If we have an imcompliete job Config ini file, then populate these fields with values from the S3 path.
            # This can be the case if we're backfilling the database with old files from the S3 bucket.
            params_dict = self.get_params_from_s3_path(job_configfile)
            jco.job_id = params_dict["job_id"]
            jco.username = params_dict["username"]
            jco.ivert_command = params_dict["command"]

        # Insert the new job into the database
        conn = self.get_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO ivert_jobs (command, username, job_id, import_prefix, export_prefix, configfile, "
            "command_args, logfile, input_dir_local, output_dir_local, job_pid, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
            (
                jco.ivert_command,
                jco.username,
                jco.job_id,
                job_import_prefix,
                job_export_prefix,
                os.path.basename(job_configfile),
                str(jco.cmd_args) if hasattr(jco, "cmd_args") else "",
                os.path.basename(job_logfile),
                job_local_dir.removeprefix(
                    self.ivert_config.ivert_jobs_directory_local
                ).lstrip("/"),
                job_local_output_dir.removeprefix(
                    self.ivert_config.ivert_jobs_directory_local
                ).lstrip("/"),
                os.getpid(),
                job_status.strip().lower(),
            ),
        )

        if update_vnum:
            self.increment_vnumber(c)

        conn.commit()

        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        existing_row = self.job_exists(jco.username, jco.job_id, return_row=True)
        return existing_row

    def populate_export_prefix_if_not_set(
        self,
        username: str,
        job_id: typing.Union[int, str],
        increment_vnum: bool = True,
        upload_to_s3: bool = True,
    ) -> str:
        """For a given job, if we're exporting files, populate the export_prefix field in the database."""
        job_row = self.job_exists(username, job_id, return_row=True)
        if not job_row:
            raise ValueError(
                f"Job ({username}, {job_id}) does not exist in the database."
            )

        if job_row["export_prefix"] is not None:
            return job_row["export_prefix"]

        # Get the export prefix from the Config file.
        export_base_prefix = self.ivert_config.s3_export_prefix_base + "jobs/"

        export_prefix = (
            export_base_prefix
            + self.ivert_config.s3_ivert_job_subdirs_template.replace(
                "[command]", job_row["command"]
            )
            .replace("[username]", job_row["username"])
            .replace("[job_id]", str(job_row["job_id"]))
            + "/"
        )

        conn = self.get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE ivert_jobs SET export_prefix = ? WHERE username = ? AND job_id = ?;",
            (export_prefix, username, job_id),
        )

        if increment_vnum:
            self.increment_vnumber(c)

        conn.commit()

        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        return export_prefix

    def create_new_file_record(
        self,
        filename: str,
        job_id: int,
        username: str,
        import_or_export: int,
        status: str = "unknown",
        upload_to_s3: bool = True,
        fake_file_stats: bool = False,
        default_file_size: int = 0,
    ) -> sqlite3.Row:
        """
        Create a new file record in the database. The (username, job_id) tuple must already exist in the ivert_jobs table.

        Args:
            filename (str): The file name being processed. Only the basename will be saved in the database (directory will be stripped).
                            The file should already exist locally (have been downloaded).
            job_id (int): The ID of the job in the "ivert_jobs" table.
            username (str): The username of the user who submitted the job.
            import_or_export (int): Whether the file is being imported (0), exported (1), or both (2).
            status (str): The initial status to assign to the job in the database. Default "unknown". Look in ivert_jobs_schema.sql for other options.
            upload_to_s3 (bool): Whether or not to upload the database after this operation is performed.
            fake_file_stats (bool): If the file doesn't exist locally, just put blank entries for the file statistics.
                                    This happens when we're just logging an error message about a file that wasn't downloaded or was quarantined.
            default_file_size (int): The default file size to use if the file doesn't exist locally.

        Returns:
            A sqlite3.Row object of the new row creatd (or the row that exists).

        Raises:
            ValueError: If a job matching the (job_id, username) pair doesn't exist in the database.
        """
        if not self.job_exists(username, job_id):
            raise ValueError(
                f"Job '{username}_{job_id} does not exist in the IVERT jobs database."
            )

        # Get just the basename of the filename. Strip any directories.
        f_basename = os.path.basename(filename)

        existing_row = self.file_exists(f_basename, username, job_id, return_row=True)
        if existing_row:
            return existing_row

        if fake_file_stats:
            # Providing fake values here is useful if there was an error processing the file and the file itself
            # doesn't exist on disk but we are still logging it as an error.
            file_md5 = "-" * 32
            file_size = default_file_size
        else:
            # If the file exists, compute the md5 and the size of it.
            file_md5 = self.s3m.compute_md5(filename)
            file_size = os.stat(filename).st_size

        status = status.strip().lower()

        conn = self.get_connection()
        cursor = conn.cursor()

        insert_query = """INSERT INTO ivert_files
                          (job_id, username, filename, import_or_export, size_bytes, md5, status)
                          VALUES (?, ?, ?, ?, ?, ?, ?);
                          """

        # Insert the new file into the database
        cursor.execute(
            insert_query,
            (
                job_id,
                username,
                f_basename,
                import_or_export,
                file_size,
                file_md5,
                status,
            ),
        )

        self.increment_vnumber(cursor)
        conn.commit()

        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

        existing_row = self.file_exists(f_basename, username, job_id, return_row=True)
        return existing_row

    def increment_vnumber(
        self,
        cursor: typing.Union[sqlite3.Cursor, None] = None,
        reset_to_zero: bool = False,
    ) -> None:
        """Increment the version number in the database.

        This is done every time a commit change is made to the database.

        If a cursor is provided, it will be used and
        the change will not be committed (the caller is responsible for committing the change).
        If not, a new cusor will be created and the change will be committed.

        Args:
            cursor (typing.Union[sqlite3.Cursor, None], optional): The cursor to use. Defaults to None.
            reset_to_zero (bool, optional): Whether to reset the version number to zero. Defaults to False.

        Returns:
            None
        """
        conn = None
        if cursor is None:
            conn = self.get_connection()
            cursor_to_use = conn.cursor()
        else:
            cursor_to_use = cursor

        if reset_to_zero:
            cursor_to_use.execute("UPDATE vnumber SET vnum = 0;")
        else:
            cursor_to_use.execute("UPDATE vnumber SET vnum = vnum + 1;")

        if cursor is None:
            conn.commit()
        return

    def set_vnumber(self, vnum: int) -> None:
        """Manually set the version number in the database."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE vnumber SET vnum = ?;", (vnum,))

        conn.commit()
        return

    def create_or_update_sns_subscription(
        self,
        username: str,
        email: str,
        topic_arn: str,
        sns_arn: str,
        sns_filter_string: typing.Union[str, None],
        increment_vnum: bool = True,
        upload_to_s3: bool = True,
    ) -> None:
        """Create a record of a new SNS subscription. If this record alredy exists, update it."""
        # First, see if this subscription already exists or not.
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM sns_subscriptions WHERE user_email = ? AND topic_arn = ? AND sns_arn = ?;",
            (email, topic_arn, sns_arn),
        )
        result = cursor.fetchone()
        if result is None:
            # The sns subscription doesn't exist. Create a new record.
            cursor.execute(
                """INSERT INTO sns_subscriptions (username, user_email, topic_arn, sns_filter_string, sns_arn) "
                           VALUES (?, ?, ?, ?, ?);""",
                (username, email, topic_arn, sns_filter_string, sns_arn),
            )
        else:
            cursor.execute(
                "UPDATE sns_subscriptions SET username = ?, sns_filter_string = ? "
                "WHERE user_email = ? AND topic_arn = ? AND sns_arn = ?;",
                (username, sns_filter_string, email, topic_arn, sns_arn),
            )

        if increment_vnum:
            self.increment_vnumber(cursor)

        conn.commit()

        if upload_to_s3:
            self.upload_to_s3()

    def remove_sns_subscription(
        self, email: str, update_vnum: bool = True, upload_to_s3: bool = True
    ) -> None:
        """Remove a record of an SNS subscription from the database.

        Note: this does not actually unsubscribe the user from the SNS topic. It just deletes the record.
        Use sns.py-->unsubscribe() to actually unsubscribe the user from the SNS topic.

        Args:
            email (str): The email address of the user to remove.
            update_vnum (bool, optional): Whether to update the version number in the database. Defaults to True.
            upload_to_s3 (bool, optional): Whether to upload the database to S3. Defaults to True.

        Returns:
            None
        """
        # First, see if this subscription already exists or not.
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT count(*) FROM sns_subscriptions WHERE user_email = ?;", (email,)
        )
        count = cursor.fetchone()[0]
        if count == 0:
            # If it doesn't exist, just return. Nothing to do here.
            return

        cursor.execute("DELETE FROM sns_subscriptions WHERE user_email = ?;", (email,))

        if update_vnum:
            self.increment_vnumber(cursor)

        conn.commit()

        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

    def create_new_sns_message(
        self,
        username: str,
        job_id: typing.Union[str, int],
        subject: str,
        sns_response: str,
        update_vnum: bool = True,
        upload_to_s3: bool = True,
    ) -> None:
        """Create a new record of an SNS message sent to an SNS topic."""

        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO sns_messages (username, job_id, subject, response) "
            "VALUES (?, ?, ?, ?);",
            (username, job_id, subject, sns_response),
        )

        if update_vnum:
            self.increment_vnumber(cursor)

        conn.commit()

        if upload_to_s3:
            self.upload_to_s3(only_if_newer=False)

    def __del__(self):
        """Destroy the object."""
        super().__del__()

    def delete_database(self) -> None:
        """Deletes the database from the S3 bucket and locally.

        Returns:
            None
        """
        # Delete the database from the S3 bucket.
        if self.s3m.exists(self.s3_database_key, bucket_type=self.s3_bucket_type):
            try:
                self.s3m.delete(self.s3_database_key, bucket_type=self.s3_bucket_type)
            except botocore.exceptions.ClientError as e:
                if e.response["Error"]["Code"] == "AccessDenied":
                    print("Access denied to delete the database from the S3 bucket.")
                else:
                    raise e

        # Delete the database locally using the JobsDatabaseClient::delete_database() method.
        super().delete_database()

    def export_single_job_data(
        self,
        username: str,
        job_id: typing.Union[str, int],
        local_directory: str,
        s3_directory: str,
        upload_to_s3: bool = True,
        verbose: bool = False,
    ) -> None:
        """Export a subset of the database, just the ivert_jobs and ivert_files data, for a single job.

        Will automatically overwrite any other subset database in the local directory.

        Args:
            username (str): The username of the user who submitted the job.
            job_id (typing.Union[str, int]): The id of the job to export.
            local_directory (str): The local directory to export the data to.
            s3_directory (str): The S3 directory to export the data to.
            upload_to_s3 (bool, optional): Whether to upload the subset database to the s3 bucket. Defaults to True.
            verbose (bool, optional): Whether to print verbose output. Defaults to False.

        Returns:
            None
        """
        # 1. Read data just for that job.
        job_data = self.read("jobs", username, job_id)
        if job_data is None or len(job_data) == 0:
            return
        assert job_data is not None and len(job_data) == 1

        file_data = self.read("files", username, job_id)
        assert file_data is not None

        # 2. Write the data to a local database file.
        base, ext = os.path.splitext(ivert_config.ivert_jobs_database_basename)
        db_fname = os.path.join(local_directory, f"{base}_{username}_{job_id}{ext}")
        if os.path.exists(db_fname):
            os.remove(db_fname)

        # Create a new database.
        conn = self.create_new_database(
            database_fname=db_fname,
            only_if_not_exists_in_s3=False,
            overwrite=True,
            verbose=verbose,
        )

        # Drop the subscriptions and messages tables, we don't need them.
        conn.execute("DROP TABLE IF EXISTS sns_subscriptions;")
        conn.execute("DROP TABLE IF EXISTS sns_messages;")

        # Get the current vnum.
        current_vnum = self.fetch_latest_db_vnum_from_database()

        # Copy the data into the new database.
        job_data.to_sql("ivert_jobs", conn, if_exists="replace", index=False)
        file_data.to_sql("ivert_files", conn, if_exists="replace", index=False)
        conn.execute(
            "INSERT OR REPLACE INTO vnumber (vnum) VALUES (?)", (current_vnum,)
        )
        conn.commit()

        # 3. Upload the local file to the s3 bucket.
        if upload_to_s3:
            s3_key = os.path.join(s3_directory, os.path.basename(db_fname))
            self.s3m.upload(
                db_fname,
                s3_key,
                bucket_type="export_server",
                delete_original=True,
                recursive=False,
                other_metadata={
                    self.s3_latest_job_metadata_key: str(job_id),
                    self.s3_vnum_metadata_key: str(current_vnum),
                    self.ivert_config.s3_jobs_db_ivert_version_metadata_key: version.__version__,
                },
            )

    def archive_database(
        self, cutoff_date_str: str = "7 days ago", verbose: bool = True
    ) -> None:
        """Truncate the database to only jobs that were created before a certain cutoff date.

        Files and jobs before that date will be copied into an archive file, of the name ivert_jobs_archive_YYYYMMDD_YYYYMMDD.db,
        with YYYYMMDD corresponding to the date of the earliest record in the database and the latest record in that archive.

        Args:
            cutoff_date_str (str, optional): The date to use as the cutoff. Defaults to "7 days ago".
            verbose (bool, optional): Whether to print verbose output. Defaults to True.

        Returns:
            None
        """
        # First, make a copy of the database.
        base, ext = os.path.splitext(self.db_fname)
        cutoff_date = dateparser.parse(cutoff_date_str).date()
        earliest_job_date_str = str(self.earliest_job_number("database"))[:-4]
        # Create the location of the archive database file.
        archive_fname = os.path.join(
            self.ivert_config.ivert_jobs_archive_dir,
            os.path.basename(
                base
                + f"_archive_{earliest_job_date_str}_{cutoff_date.year:04}{cutoff_date.month:02}{cutoff_date.day:02}"
                + ext
            ),
        )

        cutoff_date_p1 = cutoff_date
        job_id_cutoff = int(
            f"{cutoff_date_p1.year:04}{cutoff_date_p1.month:02}{cutoff_date_p1.day:02}0000"
        )

        # Create a full copy of the old database.
        conn = self.get_connection()

        # First, see if there are any jobs in the current database that are older than the cutoff date.
        # If not, there's nothing to do here.
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM ivert_jobs WHERE job_id < ?;", (job_id_cutoff,)
        )
        if cursor.fetchone()[0] == 0:
            return

        # Create the archive database as a "backup" of the existing one, then we'll truncate the tables.
        # This connection will
        conn_archive = sqlite3.connect(archive_fname)
        conn.backup(conn_archive)

        # Commit the archive backup of the current database.
        conn_archive.commit()

        # Now truncate the old database to only include files and jobs that are older or including the cutoff date.
        # Delete any files, jobs, or messages that are newer than the cutoff date.
        total_jobs_count = cursor.execute(
            "SELECT COUNT(*) FROM ivert_jobs;"
        ).fetchone()[0]
        total_files_count = cursor.execute(
            "SELECT COUNT(*) FROM ivert_files;"
        ).fetchone()[0]
        total_sns_count = cursor.execute(
            "SELECT COUNT(*) FROM sns_messages;"
        ).fetchone()[0]

        # Now truncate the old database to only include files and jobs that are older or including the cutoff date.
        # Delete any files, jobs, or messages that are newer than the cutoff date.
        cursor_archive = conn_archive.cursor()
        cursor_archive.execute(
            "DELETE FROM ivert_files WHERE job_id >= ?;", (job_id_cutoff,)
        )
        cursor_archive.execute(
            "DELETE FROM ivert_jobs WHERE job_id >= ?;", (job_id_cutoff,)
        )
        cursor_archive.execute(
            "DELETE FROM sns_messages WHERE job_id >= ?;", (job_id_cutoff,)
        )
        conn_archive.commit()
        # The "vacuum" command is used to free up disk space.
        cursor_archive.execute("VACUUM;")
        conn_archive.commit()

        # Now query how many files there are.
        old_jobs_count = cursor_archive.execute(
            "SELECT COUNT(*) FROM ivert_jobs;"
        ).fetchone()[0]
        old_files_count = cursor_archive.execute(
            "SELECT COUNT(*) FROM ivert_files;"
        ).fetchone()[0]
        old_sns_count = cursor_archive.execute(
            "SELECT COUNT(*) FROM sns_messages;"
        ).fetchone()[0]

        conn_archive.close()

        del cursor_archive
        del conn_archive

        # Now, delete all the old jobs from the current database.
        cursor.execute("DELETE FROM ivert_files WHERE job_id < ?;", (job_id_cutoff,))
        cursor.execute("DELETE FROM ivert_jobs WHERE job_id < ?;", (job_id_cutoff,))
        cursor.execute("DELETE FROM sns_messages WHERE job_id < ?;", (job_id_cutoff,))
        conn.commit()
        # The "vacuum" command is used to free up disk space.
        cursor.execute("VACUUM;")
        conn.commit()

        new_jobs_count = cursor.execute("SELECT COUNT(*) FROM ivert_jobs;").fetchone()[
            0
        ]
        new_files_count = cursor.execute(
            "SELECT COUNT(*) FROM ivert_files;"
        ).fetchone()[0]
        new_sns_count = cursor.execute("SELECT COUNT(*) FROM sns_messages;").fetchone()[
            0
        ]

        assert os.path.exists(archive_fname)
        if verbose:
            print(os.path.basename(archive_fname), "written.")
            print(
                f"{total_jobs_count:,} jobs, {total_files_count:,} files, and {total_sns_count:,} messages in {os.path.basename(self.db_fname)} (originally)."
            )
            print(
                f"{old_jobs_count:,} jobs, {old_files_count:,} files, and {old_sns_count:,} messages in {os.path.basename(archive_fname)}."
            )
            print(
                f"{new_jobs_count:,} jobs, {new_files_count:,} files, and {new_sns_count:,} messages in {os.path.basename(self.db_fname)} (now)."
            )

        # Reset the version number back to zero.
        # TODO: This will require a hard (required) update in IVERT client software.
        #   For now, keep it as is, where it doesn't reset to zero.
        #   Only enable the line below when you are confident that users will be using a new IVERT client software version.
        # self.increment_vnumber(reset_to_zero=True) # Enable only with a hard version upgrade for IVERT client.
        self.increment_vnumber()

        # Now upload the new (truncated) database to the S3, along with the cutoff date.
        self.upload_to_s3(only_if_newer=False)

        return


def define_and_parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manipulate the ivert_jobs database.")
    parser.add_argument(
        "command",
        type=str,
        choices=["create", "upload", "download", "delete", "print", "archive"],
        help="The command to execute. Choices: create, upload, download, delete, print, archive",
    )
    parser.add_argument(
        "-o",
        "--overwrite",
        dest="overwrite",
        action="store_true",
        help="Overwrite the existing database. (For create command only) Default: False",
    )
    parser.add_argument(
        "-t",
        "--table",
        dest="table",
        help="Name of the table to print to the screen. Only used for the 'print' command.",
    )
    parser.add_argument(
        "-a",
        "--all",
        dest="all",
        action="store_true",
        default=False,
        help="Print all the columns of the table. Used only with 'print -t' command.",
    )
    parser.add_argument(
        "-d",
        "--days_ago",
        dest="days_ago",
        type=int,
        default=7,
        help="When 'archive'ing, archive job & file entries older than this many days. Default: 7",
    )
    parser.add_argument(
        "-vn",
        "--vnum",
        dest="vnum",
        default="",
        help="Print the mod-version of the database, from the 'database'/'d' or 'server'/'s'. Used only with the print command.",
    )
    parser.add_argument(
        "-v",
        "--version",
        dest="version",
        action="store_true",
        default=False,
        help="Print the version of the IVERT software running on the databse. Used only with the print command.",
    )
    parser.add_argument(
        "-j",
        "--job_id",
        dest="job_id",
        type=int,
        default=None,
        help="Print only records from the given job_id. Default: Print all records.",
    )
    parser.add_argument(
        "-l",
        "--latest",
        dest="latest",
        action="store_true",
        default=False,
        help="Get the latest job number from the IVERT online database.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    if not ivert_config:
        ivert_config = configfile.Config()

    args = define_and_parse_args()

    if ivert_config.is_aws:
        idb = JobsDatabaseServer()
    else:
        idb = JobsDatabaseClient()

    if args.command == "create":
        idb.create_new_database(only_if_not_exists_in_s3=True, overwrite=args.overwrite)
    elif args.command == "upload":
        idb.upload_to_s3(only_if_newer=False)
    elif args.command == "download":
        idb.download_from_s3(only_if_newer=False)
    elif args.command == "delete":
        idb.delete_database()
    elif args.command == "archive":
        idb.archive_database(cutoff_date_str=f"{args.days_ago} days ago")
    elif args.command == "print":
        if args.vnum:
            if args.vnum[0].lower() == "d":
                print(idb.fetch_latest_db_vnum_from_database())
            else:
                print(idb.fetch_latest_db_vnum_from_s3_metadata())

        elif args.version:
            print(idb.fetch_ivert_version_from_s3_metadata())

        elif args.latest:
            print(idb.fetch_latest_job_number_from_s3_metadata())

        else:
            if args.all:
                with pandas.option_context("display.max_columns", None):
                    print(idb.read_table_as_pandas_df(args.table, job_id=args.job_id))
            else:
                print(idb.read_table_as_pandas_df(args.table, job_id=args.job_id))
