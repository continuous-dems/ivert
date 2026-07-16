#!/usr/bin/env python3

import argparse
import boto3
import botocore.exceptions
import fnmatch
import glob
import hashlib
import os
import re
import shutil
import sys
import tabulate
import tempfile
import types
import typing
import warnings

# Had to add an extra if condition because if other module entities import this.
if vars(sys.modules[__name__])["__package__"] in ("ivert", "ivert_utils"):
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.query_yes_no as query_yes_no
    import ivert_utils.bcolors as bcolors
    import ivert_utils.configfile as configfile
    import ivert_utils.progress_bar as progress_bar
    import ivert_utils.is_aws as is_aws
else:
    try:
        import utils.query_yes_no as query_yes_no
        import utils.bcolors as bcolors
        import utils.configfile as configfile
        import utils.progress_bar as progress_bar
        import utils.is_aws as is_aws
    except ModuleNotFoundError:
        import ivert_utils.query_yes_no as query_yes_no
        import ivert_utils.bcolors as bcolors
        import ivert_utils.configfile as configfile
        import ivert_utils.progress_bar as progress_bar
        import ivert_utils.is_aws as is_aws

ivert_config = None


class S3Manager:
    """Class for copying files into and out-of the IVERT AWS S3 buckets, as needed."""

    available_bucket_types = (
        "database",
        "untrusted",
        "trusted",
        "export_server",
        "export_client",
        "export_alt",
        "quarantine",
    )
    available_bucket_aliases = (
        "d",
        "u",
        "t",
        "s",
        "x",
        "xs",
        "c",
        "xc",
        "q",
        "export",
        "D",
        "U",
        "T",
        "S",
        "X",
        "XS",
        "C",
        "XC",
        "Q",
    )
    default_bucket_type = "database" if is_aws.is_aws() else "untrusted"

    def __init__(self):
        global ivert_config
        if ivert_config is None:
            ivert_config = configfile.Config()

        self.config = ivert_config

        # Different buckets for each type.
        # These are read in by the ivert_config initizliation, set at runtime.
        self.bucket_dict = {
            "database": self.config.s3_bucket_database,
            "untrusted": self.config.s3_bucket_import_untrusted,
            "trusted": self.config.s3_bucket_import_trusted,
            "export_server": self.config.s3_bucket_export_server,
            "export_client": self.config.s3_bucket_export_client,
            "export_alt": self.config.s3_bucket_export_alt,
            "quarantine": self.config.s3_bucket_quarantine,
        }

        self.endpoint_urls = {
            "untrusted": self.config.s3_import_untrusted_endpoint_url,
            "export_client": self.config.s3_export_client_endpoint_url,
            "database": None,
            "trusted": None,
            "export_server": None,
            "export_alt": self.config.s3_export_alt_endpoint_url,
            "quarantine": None,
        }

        # Different AWS profiles for each bucket. "None" indicates no profile is needed.
        # Profiles are usually needed on the user machine to use credentials for bucket access.
        self.bucket_profile_dict = dict(
            [(dbtype, None) for dbtype in self.available_bucket_types]
        )

        # Only the 'untrusted' bucket and 'export' bucket need AWS profiles, and only on the client side.
        # Grab them here.
        self.bucket_profile_dict["untrusted"] = (
            None
            if self.config.is_aws
            else self.config.aws_profile_ivert_import_untrusted
        )
        self.bucket_profile_dict["export_client"] = (
            None if self.config.is_aws else self.config.aws_profile_ivert_export_client
        )
        self.bucket_profile_dict["export_alt"] = (
            None if self.config.is_aws else self.config.aws_profile_ivert_export_alt
        )

        # The s3 session. Session created on demand when needed by the :get_resource_bucket() and :get_client() methods.
        self.session_dict = dict(
            [(dbtype, None) for dbtype in self.available_bucket_types]
        )

        # The s3 client. Client created on demand when needed by the :get_client() method.
        self.client_dict = dict(
            [(dbtype, None) for dbtype in self.available_bucket_types]
        )

        # The metadata key we use for md5 sums.
        self.md5_metadata_key = self.config.s3_md5_metadata_key

    def make_pickleable(self):
        """Make this object pickleable.

        Reset the active session and client dictionaries. Null them out."""
        self.session_dict = dict(
            [(dbtype, None) for dbtype in self.available_bucket_types]
        )
        self.client_dict = dict(
            [(dbtype, None) for dbtype in self.available_bucket_types]
        )

    def get_resource_bucket(self, bucket_type: str = None) -> boto3.resource:
        """Return and open session.resource.Bucket object for the given bucket_type."""
        if bucket_type is None:
            bucket_type = self.default_bucket_type
        bucket_type = self.convert_btype(bucket_type)

        if self.session_dict[bucket_type] is None:
            session = boto3.Session(profile_name=self.bucket_profile_dict[bucket_type])
            self.session_dict[bucket_type] = session
        else:
            session = self.session_dict[bucket_type]

        if self.bucket_dict[bucket_type] is None:
            raise ValueError(f"No bucket name assigned to '{bucket_type}'.")

        return session.resource(
            "s3", endpoint_url=self.endpoint_urls[bucket_type]
        ).Bucket(self.bucket_dict[bucket_type])

    def get_client(self, bucket_type=None) -> boto3.client:
        """Return the open client.

        If it doesn't exist yet, open one and return it."""
        if bucket_type is None:
            bucket_type = self.default_bucket_type
        bucket_type = self.convert_btype(bucket_type)

        if self.client_dict[bucket_type] is None:
            if self.session_dict[bucket_type] is None:
                session = boto3.Session(
                    profile_name=self.bucket_profile_dict[bucket_type]
                )
                self.session_dict[bucket_type] = session
            else:
                session = self.session_dict[bucket_type]

            self.client_dict[bucket_type] = session.client(
                "s3", endpoint_url=self.endpoint_urls[bucket_type]
            )

        return self.client_dict[bucket_type]

    def convert_btype(self, bucket_type) -> str:
        """Convert a user-entered bucket type into a valid value."""
        if bucket_type is None:
            bucket_type = self.default_bucket_type

        bucket_type = bucket_type.strip().lower()
        if bucket_type == "d":
            bucket_type = "database"

        elif bucket_type == "t":
            bucket_type = "trusted"

        elif bucket_type == "u":
            bucket_type = "untrusted"

        elif bucket_type in ("c", "xc"):
            if self.config.use_export_alt_bucket:
                bucket_type = "export_alt"
            else:
                bucket_type = "export_client"

        elif bucket_type in ("s", "xs"):
            if self.config.s3_bucket_export_alt:
                bucket_type = "export_alt"
            else:
                bucket_type = "export_server"

        elif bucket_type in ("x", "export"):
            if self.config.is_aws:
                if self.config.use_export_alt_bucket:
                    bucket_type = "export_alt"
                else:
                    bucket_type = "export_server"
            else:
                if self.config.use_export_alt_bucket:
                    bucket_type = "export_alt"
                else:
                    bucket_type = "export_client"

        elif bucket_type in ("q", "quarantined"):
            bucket_type = "quarantine"

        # Check to make sure this is a valid bucket name.
        if bucket_type not in self.available_bucket_types:
            raise ValueError(
                f"Unknown bucket type '{bucket_type}'. Must be one of {self.available_bucket_types} or {self.available_bucket_aliases}."
            )

        return bucket_type

    def get_bucketname(self, bucket_type=None):
        bucket_type = self.convert_btype(bucket_type)

        if bucket_type.lower() not in self.bucket_dict.keys():
            # Try to see if it's a valid bucket name already, rather than a bucket_type.
            raise ValueError(
                f"Unknown bucket type '{bucket_type}'. Must be one of {list(self.bucket_dict.keys())}."
            )

        return self.bucket_dict[bucket_type]

    def verify_same_size(self, filename, s3_key, bucket_type=None):
        """Return True if the local file is the exact same size in bytes as the S3 key."""
        bucket_type = self.convert_btype(bucket_type)

        head = self.exists(s3_key, bucket_type=bucket_type, return_head=True)
        if head is False:
            return False

        if "content-length" in head.keys():
            s3_size = int(head["content-length"])
        elif "content-length" in head["ResponseMetadata"]["HTTPHeaders"].keys():
            s3_size = int(head["ResponseMetadata"]["HTTPHeaders"]["content-length"])
        else:
            return False

        if os.path.exists(filename):
            local_size = os.stat(filename).st_size
        else:
            return False

        return s3_size == local_size

    def size(self, s3_key, bucket_type=None):
        """Return the size of the S3 key."""
        bucket_type = self.convert_btype(bucket_type)

        head = self.exists(s3_key, bucket_type=bucket_type, return_head=True)
        if head is False:
            return False

        if "content-length" in head.keys():
            return int(head["content-length"])
        elif "content-length" in head["ResponseMetadata"]["HTTPHeaders"].keys():
            return int(head["ResponseMetadata"]["HTTPHeaders"]["content-length"])
        else:
            return False

    def exists(self, s3_key, bucket_type=None, return_head: bool = False):
        """Look in the appropriate bucket, and see if a file or directory exists there."""
        bucket_type = self.convert_btype(bucket_type)

        client = self.get_client(bucket_type=bucket_type)

        bucket_name = self.get_bucketname(bucket_type=bucket_type)

        if s3_key == "/":
            s3_key = ""

        try:
            head = client.head_object(Bucket=bucket_name, Key=s3_key)
            if return_head:
                return head
            else:
                return True

        except botocore.exceptions.ClientError as e:
            try:
                if self.is_existing_s3_directory(s3_key, bucket_type=bucket_type):
                    return True
            except botocore.exceptions.ClientError:
                pass

            if e.response["Error"]["Code"] == "404":
                return False
            elif e.response["Error"]["Code"] == "403" and e.response["Error"][
                "Message"
            ].lower() in ("access denied", "accessdenied", "forbidden"):
                warnings.warn(
                    f"Warning: Access denied to s3://{bucket_name}/{s3_key}. Response: {e.response}"
                    "\nAre your credentials up to date? If not, try running 'ivert setup' again with the "
                    "latest credentials file."
                    "\nIf that doesn't work, talk to your IVERT administrator for help."
                )
                return False

            else:
                warnings.warn(
                    f"Warning: Unknown error fetching status of s3://{bucket_name}/{s3_key}. "
                    f"Response: {e.response}"
                )
                return False

    def is_existing_s3_directory(self, s3_key, bucket_type=None):
        """Return True if 'key' points to an existing directory (prefix) in the bucket. NOT a file. False otherwise."""
        bucket_type = self.convert_btype(bucket_type)

        try:
            bucket_obj = self.get_resource_bucket(bucket_type=bucket_type)

            if bucket_obj and s3_key in (".", "", "/"):
                return True

            # Filter the list of objects by the prefix. If the prefix doesn't exist, this will be empty.
            objects = bucket_obj.objects.filter(Prefix=s3_key)

            # Loop through the objects returned.
            for i, obj in enumerate(objects):
                # If it's an exact match with the key, then it's a file (not a directory). Return False.
                if obj.key == s3_key:
                    return False

                # Otherwise, the key should be the start of the object.
                assert obj.key.find(s3_key) == 0

                # If we match with an object and the character immediately after the prefix is a '/', then it's a
                # directory.
                # If some other character is there, then we're not sure yet, move along to the next object.
                if s3_key[-1] == "/" or obj.key[len(s3_key)] == "/":
                    return True

            return False

        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            elif e.response["Error"]["Code"] == "403" and e.response["Error"][
                "Message"
            ].lower() in ("access denied", "accessdenied", "forbidden"):
                warnings.warn(
                    f"Warning: Access denied to s3://{self.bucket_dict[bucket_type]}/{s3_key}. "
                    f"Response: {e.response}"
                    "\nAre your credentials up to date? If not, try running 'ivert setup' again with the "
                    "latest credentials file."
                    "\nIf that doesn't work, talk to your IVERT administrator for help."
                )
                return False
            else:
                warnings.warn(
                    "Warning: Unknown error fetching status of "
                    f"s3://{self.bucket_dict[bucket_type]}/{s3_key}. Response: {e.response}"
                )
                return False

    def download(
        self,
        s3_key: str,
        filename: str,
        bucket_type: str = None,
        delete_original: bool = False,
        recursive: bool = False,
        fail_quietly: bool = True,
        show_progress_bar: bool = False,
        use_tempfile: bool = False,
        include_metadata: bool = False,
    ) -> list:
        """Download a file from the S3 to the local file system.

        Args:
            s3_key (str): The S3 key to download.
            filename (str): The local filename to download to.
            bucket_type (str): The type of bucket to download from.
            delete_original (bool): Whether to delete the original file after downloading.
            recursive (bool): Whether to download recursively.
            fail_quietly (bool): Whether to fail quietly if the file doesn't exist.
            show_progress_bar (bool): Whether to show a progress bar.
            use_tempfile (bool): Whether to use a temporary file and then copy over after the download is complete.
            include_metadata (bool): Whether to include the metadata in the downloaded file. In this case, the list of
                                     files will be a list of (filename, metadata_dict) tuples."""
        bucket_type = self.convert_btype(bucket_type)

        client = self.get_client(bucket_type=bucket_type)

        bucket_name = self.get_bucketname(bucket_type=bucket_type)

        assert not self.contains_glob_flags(filename)

        if self.contains_glob_flags(s3_key):
            base_key = self.get_base_directory_before_glob(s3_key)
            all_s3_keys = self.listdir(
                base_key, bucket_type=bucket_type, recursive=recursive
            )
            matching_s3_keys = fnmatch.filter(all_s3_keys, s3_key)
            s3_keys_to_download = matching_s3_keys
        else:
            s3_keys_to_download = [s3_key]

        files_downloaded = []
        for i, s3k in enumerate(s3_keys_to_download):
            # If the 'filename' given is a directory, use the same filename as the key, put the file in that directory.
            if os.path.isdir(filename):
                filename_to_download = os.path.join(filename, s3k.split("/")[-1])
            elif len(s3_keys_to_download) == 1:
                filename_to_download = filename
            else:
                raise ValueError(
                    f"'{filename}' must be an existing directory if there are multiple keys to download."
                )

            if use_tempfile:
                with tempfile.NamedTemporaryFile(delete=False) as tf:
                    tfname = tf.name
                    client.download_file(bucket_name, s3k, tfname)
                    shutil.copy(tfname, filename_to_download)
                    os.remove(tfname)
            else:
                client.download_file(bucket_name, s3k, filename_to_download)

            if not self.verify_same_size(
                filename_to_download, s3k, bucket_type=bucket_type
            ):
                if fail_quietly:
                    continue
                else:
                    raise RuntimeError(
                        f"Error: S3_Manager.download() failed to download file {s3k.split('/')[-1]} correctly."
                    )

            if include_metadata:
                metadata = client.head_object(Bucket=bucket_name, Key=s3k)
                filename_to_download = (filename_to_download, metadata)

            files_downloaded.append(filename_to_download)

            # Delete the original file from the s3 if requested (such as in a 'mv' command)
            if delete_original:
                client.delete_object(Bucket=bucket_name, Key=s3k)

            if show_progress_bar:
                progress_bar.ProgressBar(
                    i + 1,
                    len(s3_keys_to_download),
                    suffix=f"{i + 1}/{len(s3_keys_to_download)}",
                    decimals=0,
                )

        return files_downloaded

    def upload(
        self,
        filename: str,
        s3_key: str,
        bucket_type: str = None,
        delete_original: bool = False,
        fail_quietly: bool = True,
        recursive: bool = False,
        include_md5: bool = False,
        other_metadata: dict = None,
    ):
        """Upload a file from the local file system to the S3.

        Args:
            filename (str): The local file to upload.
            s3_key (str): The S3 key to upload to.
            bucket_type (str): The type of bucket to upload to. Default to 'database'.
            delete_original (bool): Whether to delete the original file after uploading. This would be like the 'mv'
                                    command rather than 'cp'. Default to False.
            fail_quietly (bool): How to fail if the upload fails. If fail_quietly is True, return False.
                                 Else, raise an error. Default to True.
            recursive (bool): If using a wildcard to grab multiple files, whether to upload all matching files in the
                              directory including recursively in subdirectories. Default to False (match only the
                              base directory).
            include_md5 (bool): Whether to include the md5 hash of the file in the metadata. Default to False.
            other_metadata (dict): Other metadata to include in the s3 object header. Default to {}."""
        bucket_type = self.convert_btype(bucket_type)

        client = self.get_client(bucket_type=bucket_type)

        bucket_name = self.get_bucketname(bucket_type=bucket_type)

        assert not self.contains_glob_flags(s3_key)

        # Expand user home directory if needed.
        filename = os.path.expanduser(filename)

        if self.contains_glob_flags(filename):
            matching_filenames = [
                fn
                for fn in glob.glob(filename, recursive=recursive)
                if not os.path.isdir(fn)
            ]
        else:
            matching_filenames = [filename]

        if len(matching_filenames) == 0:
            return []

        if other_metadata is None:
            other_metadata = {}

        files_uploaded = []
        for fname in matching_filenames:
            # If the key is pointing to an S3 directory (prefix), then use the same filename as filename, and put it
            # in that directory.
            if (
                self.is_existing_s3_directory(s3_key, bucket_type=bucket_type)
                or s3_key[-1] == "/"
            ):
                s3_key = (s3_key + "/" + os.path.basename(fname)).replace("//", "/")

            # If we want to include the md5 hash of the file in the metadata, then compute it.
            if include_md5:
                md5_hash = self.compute_md5(fname)
                other_metadata[self.md5_metadata_key] = md5_hash

            # Upload the file. Include other metadata if it's not empty.
            client.upload_file(
                fname,
                bucket_name,
                s3_key,
                ExtraArgs=None
                if (other_metadata is None or len(other_metadata) == 0)
                else {"Metadata": other_metadata},
            )

            if not self.verify_same_size(fname, s3_key, bucket_type=bucket_type):
                if fail_quietly:
                    return False
                else:
                    raise RuntimeError(
                        "Error: S3_Manager.upload() failed to upload file correctly."
                    )

            files_uploaded.append(s3_key)

            if delete_original:
                os.remove(fname)

        return files_uploaded

    def transfer(
        self,
        src_key: str,
        dst_key: str,
        src_bucket_type: str = None,
        dst_bucket_type: str = None,
        recursive: bool = False,
        delete_original: bool = False,
        fail_quietly: bool = True,
        include_metadata: bool = True,
    ) -> bool:
        """Move or copy a file or set of files between two S3 buckets.

        Args:
            src_key (str): The S3 source key.
            dst_key (str): The S3 destination key. If an existing directory, will transfer to that directory
                           (same filename).
            dst_bucket_type (str): The destination bucket type.
            src_bucket_type (str): The source bucket type. Default to 'database' if on the EC2 or 'untrusted' if on a
                                   client.
            recursive (bool): If wildcards are used in s3_key1, whether to match filesname recursively in subdirectories.
            delete_original (bool): Whether to delete the original file after uploading. This would be like the 'mv'
                                    command rather than 'cp'. Default to False.
            fail_quietly (bool): How to fail if the upload fails. If fail_quietly is True, return False.
                                 Else, raise an error. Default to True.
            include_metadata (bool): Whether to include the source metadata in the transfer.

        Returns:
            bool: Whether the transfer was successful.

            Caution: This function has not yet been thoroughly tested. It may not work on all cases."""

        dst_bucket_type = self.convert_btype(dst_bucket_type)
        src_bucket_type = self.convert_btype(src_bucket_type)

        if self.contains_glob_flags(src_key):
            matching_source_keys = self.listdir(
                src_key, bucket_type=src_bucket_type, recursive=recursive
            )
            # Check to make sure it's a directory.
            if len(matching_source_keys) > 1 and not self.is_existing_s3_directory(
                dst_key, bucket_type=dst_bucket_type
            ):
                raise ValueError(
                    "The destination key must be a directory if the source key refers to more than one file."
                )
        else:
            matching_source_keys = [src_key]

        client = self.get_client(bucket_type=dst_bucket_type)

        for s_key in matching_source_keys:
            if self.is_existing_s3_directory(dst_key, bucket_type=dst_bucket_type):
                d_key = dst_key.rstrip("/") + "/" + os.path.basename(s_key)
            else:
                d_key = dst_key

            # If we are accessing two different buckets with two different sets of AWS credentials, then we need to
            # transfer using a temporary file on this machine. Download, then upload. This only applies if the source
            # bucket uses an AWS profile at all.
            if (
                self.bucket_profile_dict[src_bucket_type] is not None
                and self.bucket_profile_dict[src_bucket_type]
                != self.bucket_profile_dict[dst_bucket_type]
            ):
                temp_file = tempfile.NamedTemporaryFile()
                f_downloads = self.download(
                    s_key,
                    temp_file.name,
                    bucket_type=src_bucket_type,
                    delete_original=False,
                    fail_quietly=fail_quietly,
                )

                # Fetch the metdata if we're copying it over.
                if include_metadata:
                    md_record = self.get_metadata(s_key, bucket_type=src_bucket_type)
                else:
                    md_record = None

                if not f_downloads:
                    if fail_quietly:
                        return False
                    else:
                        raise RuntimeError(
                            f"Error: S3Manager.transfer() failed to transfer file {s_key} correctly."
                        )

                self.upload(
                    temp_file.name,
                    d_key,
                    bucket_type=dst_bucket_type,
                    delete_original=False,
                    fail_quietly=fail_quietly,
                    other_metadata=md_record,
                )

                # Confirm the file uploaded correctly
                if self.exists(d_key, bucket_type=dst_bucket_type):
                    # Delete the original file if needed.
                    if delete_original:
                        self.delete(s_key, bucket_type=src_bucket_type)
                else:
                    if fail_quietly:
                        return False
                    else:
                        raise RuntimeError(
                            f"Error: S3Manager.transfer() failed to transfer file {d_key} to the '{dst_bucket_type}' "
                            "bucket."
                        )

                # Closing a tempfile deletes it.
                temp_file.close()
                continue

            # If they don't require credentials, just copy the file without download using S3's CopyObject API
            try:
                client.copy_object(
                    Bucket=self.get_bucketname(bucket_type=dst_bucket_type),
                    CopySource={
                        "Bucket": self.get_bucketname(bucket_type=src_bucket_type),
                        "Key": s_key,
                    },
                    Key=d_key,
                    MetadataDirective="COPY" if include_metadata else "REPLACE",
                )

                if delete_original and self.exists(d_key, bucket_type=dst_bucket_type):
                    self.delete(s_key, bucket_type=src_bucket_type)

            except Exception as e:
                if fail_quietly:
                    return False
                else:
                    raise e

        return True

    def listdir(
        self, s3_key: str, bucket_type: str = None, recursive: bool = False
    ) -> list:
        """List all the files within a given directory.

        Returns the full key path, since that's how S3's operate. But this make it a bit different than os.listdir().
        """
        bucket_type = self.convert_btype(bucket_type)

        # Directories should not start with '/'
        if len(s3_key) > 0 and s3_key[0] == "/":
            s3_key = s3_key[1:]

        # If we're using glob flags, return all the matching keys
        if self.contains_glob_flags(s3_key):
            s3_base_key = self.get_base_directory_before_glob(s3_key)
            # For this sub-search only, we're not traversing subdirectories and we don't want the metadata.
            s3_matches_all = self.listdir(
                s3_base_key, bucket_type=bucket_type, recursive=True
            )
            s3_matches = fnmatch.filter(s3_matches_all, s3_key)
            if recursive:
                return s3_matches
            else:
                # Get the files that are in that directory (and not in subdirectories)
                fmatches = [
                    m
                    for m in s3_matches
                    if m[len(s3_base_key) :].lstrip("/").find("/") == -1
                ]

                # Get the directories that are in that directory
                dirmatches = sorted(
                    list(
                        set(
                            [
                                m[: len(s3_base_key)].rstrip("/")
                                + "/"
                                + m[len(s3_base_key) :].lstrip("/").split("/")[0]
                                + "/"
                                for m in s3_matches
                                if m[len(s3_base_key) :].lstrip("/").find("/") > -1
                            ]
                        )
                    )
                )
                return dirmatches + fmatches

        # If no glob flags, make sure it's actually a directory (or existing file) we're looking at, not just a partial
        # substring.
        if not self.is_existing_s3_directory(s3_key, bucket_type=bucket_type):
            if self.exists(s3_key):
                return [s3_key]
            else:
                raise FileNotFoundError(
                    f"Error: '{s3_key}' is not a file or directory in bucket "
                    f"'{self.get_bucketname(bucket_type=bucket_type)}'."
                )

        # Make sure the directory ends with a '/'.
        if len(s3_key) > 0 and not s3_key.endswith("/"):
            s3_key = s3_key + "/"

        # Using a paginator helps us get more than 1000 objects.
        client = self.get_client(bucket_type=bucket_type)
        paginator = client.get_paginator("list_objects_v2")

        if recursive:
            # Query all the keys in batches of 1000, ignoring delimiters
            pages = paginator.paginate(
                Bucket=self.get_bucketname(bucket_type=bucket_type), Prefix=s3_key
            )

            # Get all the keys from each page, for all the pages in the paginator
            return [obj["Key"] for page in pages for obj in page["Contents"]]

        else:
            # Query all the keys in batches of 1000, but cut off with the delimiter
            pages = paginator.paginate(
                Bucket=self.get_bucketname(bucket_type=bucket_type),
                Prefix=s3_key,
                Delimiter="/",
            )

            subdirs = []
            files = []

            for page in pages:
                # Subdirectories are listed as "CommonPrefixes", while files are listed as "Contents".
                if "CommonPrefixes" in page.keys():
                    subdirs.extend(
                        [subdir["Prefix"] for subdir in page["CommonPrefixes"]]
                    )

                if "Contents" in page.keys():
                    files.extend([f["Key"] for f in page["Contents"]])

            return subdirs + files

    def delete(
        self, s3_key, bucket_type=None, recursive=False, max_without_warning=100
    ):
        """Delete a key (file) from the S3."""

        bucket_type = self.convert_btype(bucket_type)

        client = self.get_client(bucket_type=bucket_type)
        bucket_name = self.get_bucketname(bucket_type=bucket_type)

        if self.contains_glob_flags(s3_key):
            if recursive and s3_key.strip() == "*":
                raise ValueError(
                    "Error: Can't recursively delete everything in a bucket."
                )

            matching_keys = self.listdir(
                s3_key, bucket_type=bucket_type, recursive=recursive
            )

            # Don't delete more than max_without_warning without user confirmation
            if (
                max_without_warning is not None
                and len(matching_keys) > max_without_warning
            ):
                query_prompt = f"This will delete {len(matching_keys)} files in bucket '{bucket_name}'. Continue?"
                delete_all_confirmation = query_yes_no.query_yes_no(
                    query_prompt, default="no"
                )

                # If the user doesn't confirm, don't delete anything, just exit.
                if not delete_all_confirmation:
                    return

            # Use the client.delete_objects() method to delete multiple objects at once
            # Loop over 1000 objects at a time (the AWS maximum for delete_objects())
            for i in range(0, len(matching_keys), 1000):
                # Get the next chunk of 1000 keys, or less if we're at the end
                chunk_max_i = min(i + 1000, len(matching_keys))
                client.delete_objects(
                    Bucket=bucket_name,
                    Delete={
                        "Objects": [{"Key": k} for k in matching_keys[i:chunk_max_i]]
                    },
                )

        else:
            client.delete_object(Bucket=bucket_name, Key=s3_key)

        return

    @staticmethod
    def contains_glob_flags(s3_key):
        """Return True if a string contains any glob-style wildcard flags."""

        return ("*" in s3_key) or ("?" in s3_key) or ("[" in s3_key and "]" in s3_key)

    @staticmethod
    def get_base_directory_before_glob(s3_key):
        """Return the base directory before any glob-style wildcard flags in an S3 key."""

        dirs = s3_key.split("/")
        basedirs = []
        for dname in dirs:
            if "*" in dname or "?" in dname or "[" in dname:
                break
            basedirs.append(dname)

        return "/".join(basedirs)

    @staticmethod
    def compute_md5(filename):
        """Compute the md5 hash of a file."""

        md5 = hashlib.md5()
        with open(filename, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                md5.update(chunk)
        return md5.hexdigest()

    def get_md5(
        self,
        s3_key: str,
        bucket_type: typing.Union[str, None] = None,
        use_tags: bool = False,
    ):
        """Return the md5 hash of an S3 key, if it was provided when uploaded.

        NOTE: This is different from the AWS object.content_md5 key, which is computed differently and may not
        match a locally-computed md5 sum."""

        bucket_type = self.convert_btype(bucket_type)

        metadata = self.get_metadata(
            s3_key,
            bucket_type=bucket_type,
            recursive=False,
            return_entire_header=False,
            use_tags=use_tags,
        )[self.config.s3_md5_metadata_key]
        return metadata

    def compare_md5(
        self,
        filename: str,
        s3_key: str,
        bucket_type: bool = None,
        use_tags: bool = False,
    ) -> bool:
        """Return True if the local file's md5 matches the md5 in the S3 key.

        If the S3 key does not have an md5 in its metadata, return False."""

        s3_md5 = self.get_md5(s3_key, bucket_type=bucket_type, use_tags=use_tags)
        if s3_md5 is None or s3_md5 == "":
            return False
        else:
            return self.compute_md5(filename) == s3_md5

    def get_metadata(
        self,
        s3_key: str,
        bucket_type: str = None,
        recursive: bool = False,
        return_entire_header: bool = False,
        use_tags: typing.Union[bool, None] = None,
    ) -> typing.Dict:
        """Return the user-defined metadata of an S3 key.

        If wildcards are used, return it as a {key: metadata_dict}" dictionary, even if only one file is matched.

        Args:
            s3_key (str): The S3 key to get the metadata of.
            bucket_type (str, optional): The type of bucket to use. Defaults to None.
            recursive (bool, optional): Whether to recurse into subdirectories. Defaults to False. Only applies if
                                        wildcard flags are used.
            return_entire_header (bool, optional): Whether to return the entire header, or just the user-defined
                                                   'Metadata' portion of it. Defaults to False (return just 'Metadata').
                                                   Only applies if use_tags is False.
            use_tags (bool, optional): Whether to return the user-defined 'Tags' portion of the header. Defaults to
                                       None. If None, only use tags on the export bucket if
                                       ivert_config.ivert_export_client_use_aws_tags_instead_of_metadata is True.

        Returns:
            typing.Dict: The metadata of the S3 key.
        """

        bucket_type = self.convert_btype(bucket_type)

        if self.contains_glob_flags(s3_key):
            keyvals = {}
            key_names = self.listdir(
                s3_key, bucket_type=bucket_type, recursive=recursive
            )

            for key in key_names:
                # If we're not recursing, directories can be returned as well. Ignore those.
                # Otherwise, add the key to the dictionary by calling this function recursively on the non-globbed match.
                if not self.is_existing_s3_directory(key, bucket_type=bucket_type):
                    keyvals[key] = self.get_metadata(
                        key,
                        bucket_type=bucket_type,
                        recursive=False,
                        return_entire_header=return_entire_header,
                    )

            return keyvals

        else:
            client = self.get_client(bucket_type=bucket_type)

            bname = self.get_bucketname(bucket_type=bucket_type)
            # If use_tags wasn't set, set it to the export_client bucket's setting in the ivert_config file.
            # This is the only IVERT bucket in which this is applicable.
            if use_tags is None:
                if bucket_type == "export_client":
                    use_tags = (
                        self.config.ivert_export_client_use_aws_tags_instead_of_metadata
                    )
                else:
                    use_tags = False

            # If using tags, use the s3api "get-object-tagging" feature, under "TagSet".
            # It returns a list of dicts with "Key" and "Value" keys... convert to a key-value pair single dictionary.
            if use_tags:
                head = client.get_object_tagging(Bucket=bname, Key=s3_key)
                return dict([(t["Key"], t["Value"]) for t in head["TagSet"]])

            # Otherwise, use the s3api "head-object" feature, under "Metadata".
            else:
                head = client.head_object(Bucket=bname, Key=s3_key)
                if return_entire_header:
                    return head
                else:
                    return head["Metadata"]


def pretty_print_bucket_list(use_formatting=True):
    """Prints a table of the available buckets and the prefixes used.

    This is formatted for printing to a bash command shell.
    """
    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

    aliases = S3Manager.available_bucket_types

    bname_dict = {
        "database": ivert_config.s3_bucket_database,
        "trusted": ivert_config.s3_bucket_import_trusted,
        "untrusted": ivert_config.s3_bucket_import_untrusted,
        "export_client": ivert_config.s3_bucket_export_client,
        "export_server": ivert_config.s3_bucket_export_server,
        "export_alt": ivert_config.s3_bucket_export_alt,
        "quarantine": ivert_config.s3_bucket_quarantine,
    }

    prefixes_dict = {
        "database": "",
        "trusted": ivert_config.s3_import_trusted_prefix_base
        if bname_dict["trusted"]
        else "",
        "untrusted": ivert_config.s3_import_untrusted_prefix_base
        if bname_dict["untrusted"]
        else "",
        "export_client": ivert_config.s3_export_client_prefix_base
        if bname_dict["export_client"]
        else "",
        "export_server": ivert_config.s3_export_server_prefix_base
        if bname_dict["export_server"]
        else "",
        "export_alt": ivert_config.s3_export_alt_prefix_base
        if bname_dict["export_alt"]
        else "",
        "quarantine": ivert_config.s3_quarantine_prefix_base
        if bname_dict["quarantine"]
        else "",
    }

    bc = bcolors.bcolors()

    # A flag to incidate whether we had any "None" values.
    none_values_found = False
    none_str = ""

    # Convert "None" values in database names to italic <None>
    for key in aliases:
        if bname_dict[key] is None:
            none_str = "None"
            if use_formatting:
                none_str = bc.ITALIC + none_str + bc.ENDC
            bname_dict[key] = none_str
            none_values_found = True

    # Create the table
    if use_formatting:
        data = [
            [
                bc.OKBLUE + bc.BOLD + key + bc.ENDC + bc.ENDC,
                bname_dict[key],
                prefixes_dict[key],
            ]
            for key in aliases
        ]
    else:
        data = [[key, bname_dict[key], prefixes_dict[key]] for key in aliases]
    if use_formatting:
        headers = [
            bc.HEADER + txt + bc.ENDC for txt in ["Alias", "S3 Bucket", "Prefix Used"]
        ]
    else:
        headers = ["Alias", "S3 Bucket", "Prefix Used"]

    # Add * to the default bucket alias
    for i, key in enumerate(aliases):
        if key == S3Manager.default_bucket_type:
            data[i][0] = bc.BOLD + "*" + bc.ENDC + data[i][0]

    # Print
    print()
    print(
        tabulate.tabulate(
            data, headers=headers, colalign=["right", "left", "left"], tablefmt="plain"
        )
    )

    print(f"\n{bc.BOLD}*{bc.ENDC}default bucket on this machine.\n")
    if none_values_found:
        print(
            f"{bc.BOLD}Note{bc.ENDC}: '{none_str}' indicates that the bucket is not used by this client and/or is "
            f"not set in the Config file. For instance, the {bc.OKBLUE}{bc.BOLD}database{bc.ENDC}{bc.ENDC} and "
            f"{bc.OKBLUE}{bc.BOLD}trusted{bc.ENDC}{bc.ENDC} buckets are used by the EC2 server and are not set in "
            f"a user's Config file. ({bc.ITALIC}This is fine.{bc.ENDC})\n"
        )


def add_subparser_bucket_param(subparser):
    """All the sub-parsers use the same bucket optional argument. Add it here."""

    subparser.add_argument(
        "--bucket",
        "-b",
        default=None,
        choices=list(
            S3Manager.available_bucket_types + S3Manager.available_bucket_aliases
        )
        + [None],
        help="Shorthand alias of the src S3 bucket. "
        "Options are: 'database' 'd' (server work bucket, only accessible within the S3); "
        "'trusted' 't' (files that passed secure ingest, only accessible within the S3); "
        "'untrusted' 'u' (files uploaded to IVERT, only accessible by the client using credentials); "
        "'all' 'a' (all of the above).",
        type=str,
    )


def define_and_parse_args_v2(just_return_parser: bool = False) -> argparse.Namespace:
    """Parse command-line arguments using sub-parsers in python."""

    #####################################################
    #################### MAIN parser ####################
    #####################################################
    parser = argparse.ArgumentParser(
        description="Quick python utility for interacting with IVERT's S3 buckets."
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help=f"The command to run. "
        f"Run '{os.path.basename(__file__)} <command> --help' for options of each command.",
        required=True,
    )

    # Add a helpful output message if we get parsing errors.
    def custom_error_main(self, default_msg):
        extra_msg = "For more details type 'python {0} --help' or 'python {0} <command> --help'.".format(
            os.path.basename(__file__)
        )
        print(f"{os.path.basename(__file__)} error:", default_msg + "\n" + extra_msg)

    parser.error = types.MethodType(custom_error_main, parser)

    #####################################################
    #################### 'ls' parser ####################
    #####################################################
    # The ls (list files) parser
    parser_ls = subparsers.add_parser(
        "ls",
        description="List files in an S3 bucket.",
        help="List files in an S3 bucket.",
        add_help=True,
    )
    parser_ls.add_argument(
        "key",
        default=".",
        help="The directory/prefix or file/key to list from the S3. "
        "Wildcards (e.g. ./ncei19*.tif) allowed. "
        "Use '.' or '/' to quiery the base prefix for the S3 bucket. Default: .",
    )
    add_subparser_bucket_param(parser_ls)
    parser_ls.add_argument(
        "-r",
        "--recursive",
        dest="recursive",
        action="store_true",
        default=False,
        help="List files recursively (including all keys in subdirectories).",
    )
    parser_ls.add_argument(
        "-m",
        "--meta",
        "--metadata",
        dest="metadata",
        action="store_true",
        default=False,
        help="List files with all their metadata that was recorded in the s3 metadata during upload. "
        "(NOT YET IMPLEMENTED)",
    )
    parser_ls.add_argument(
        "-md5",
        "--md5",
        dest="md5",
        action="store_true",
        default=False,
        help="List files with their md5 hashes if recorded in the s3 metadata during upload. "
        "(NOT YET IMPLEMENTED)",
    )

    def custom_error_ls(self, default_msg):
        extra_msg = "For more details type 'python {0} ls --help'".format(
            os.path.basename(__file__)
        )
        print(f"{os.path.basename(__file__)} ls error:", default_msg + "\n" + extra_msg)

    parser_ls.error = types.MethodType(custom_error_ls, parser_ls)

    #####################################################
    #################### 'rm' parser ####################
    #####################################################
    # The rm (remove) parser
    parser_rm = subparsers.add_parser(
        "rm",
        description="Remove files from an S3 bucket.",
        help="Remove files from an S3 bucket.",
        add_help=True,
    )
    parser_rm.add_argument(
        "key",
        default=".",
        help="The directory/prefix or file/key to remove from the S3. "
        "Wildcards (e.g. ./ncei19*.tif) allowed. "
        "Use '.' or '/' to quiery the base prefix for the S3 bucket. Default: .",
    )
    add_subparser_bucket_param(parser_rm)
    parser_rm.add_argument(
        "-r",
        "--recursive",
        dest="recursive",
        action="store_true",
        default=False,
        help="If a direcrory is specified with a wildcard, remove files recursively "
        "(including all files in sub-directories). "
        "For safety, deleting all files in a bucket with the '*' wildcard and '-r' is not allowed.",
    )
    parser_rm.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        default=False,
        help="By default the command will ask for confirmation before deleting more than 100 files. "
        "Override this. (Be careful here!)",
    )

    def custom_error_rm(self, default_msg):
        extra_msg = "For more details type 'python {0} rm --help'".format(
            os.path.basename(__file__)
        )
        print(f"{os.path.basename(__file__)} rm error:", default_msg + "\n" + extra_msg)

    parser_rm.error = types.MethodType(custom_error_rm, parser_rm)

    ##########################################################
    #################### 'cp'/'mv' parser ####################
    ##########################################################
    # The cp (copy) and mv (move) parser
    # Since 'cp' and 'mv' are both very similar (only difference is whether the original is deleted), we'll use the same parser.
    parser_cp = subparsers.add_parser(
        "cp",
        aliases=["mv"],
        description="Copy (or move) files to, from, or between S3 buckets. "
        "At least one of 'key1' and 'key2' must contain an s3 prefix 's3:[key]' or 's3://[key]'. "
        "Do not include the bucket name with s3:[key], that is inserted by the --bucket argument "
        "which can use the default bucket if not specified.",
        help="Copy (or move) files to, from, or between S3 buckets. ",
        add_help=True,
    )
    parser_cp.add_argument(
        "key1",
        help="The source file/key to copy/move from the s3 or the local file system. "
        "Wildcards (e.g. ./ncei19*.tif) allowed if the second argument is a directory. "
        "To specify an S3 key (i.e. to download), use the 's3:' or 's3://' prefix, case-insensitive. "
        "Unlike aws commands, the bucket-name is not provided here "
        "(it is specified in the --bucket alias argument).",
    )
    parser_cp.add_argument(
        "key2",
        help="The file/key to copy/move to the s3 or the local file system. "
        "If an existing directory is given, the filename will remain the same. "
        "To specify an S3 key (i.e. to upload), use the 's3:' or 's3://' prefix. Unlike aws "
        "commands, the bucket-name is not provided here (specified in the --bucket alias argument).",
    )
    add_subparser_bucket_param(parser_cp)
    parser_cp.add_argument(
        "-r",
        "--recursive",
        dest="recursive",
        action="store_true",
        default=False,
        help="If a direcrory is specified with a wildcard, copy/move files recursively "
        "(including all files in sub-directories). "
        "For safetly, if a '*' wildcare is used as the key along with '-r', a confirmation "
        "a warning will be issued to the user before removing all contents of the bucket.",
    )
    parser_cp.add_argument(
        "-m",
        "--meta",
        "--metadata",
        dest="metadata",
        metavar="KEY=VALUE",
        nargs="+",
        default=dict(),
        help="For uploads only: Add one or more metadata values to the S3 metadata for the file(s) in the "
        "destination S3 bucket. Args should be listed as key=value pairs. Ignored for downloads.",
    )
    parser_cp.add_argument(
        "-md5",
        "--md5",
        dest="md5",
        action="store_true",
        default=False,
        help="Add the MD5 checksum to the S3 metadata for the file(s) in the destination S3 bucket. "
        "Ignored for downloads.",
    )
    parser_cp.add_argument(
        "-db",
        "--dest_bucket",
        dest="dest_bucket",
        metavar="BUCKET",
        default=None,
        help="If moving between buckets, the name of the S3 bucket to copy/move the file(s) to if "
        "different from the original bucket. "
        "Default: Same as source bucket.",
    )

    # Custom error function for 'cp' and 'mv'
    def custom_error_cp(self, default_msg):
        extra_msg = "For more details type 'python {0} {{cp,mv}} --help'".format(
            os.path.basename(__file__)
        )
        print(
            f"{os.path.basename(__file__)} cp/mv error:", default_msg + "\n" + extra_msg
        )

    parser_cp.error = types.MethodType(custom_error_cp, parser_cp)

    #############################################################
    #################### 'buckets' parser #######################
    #############################################################

    parser_buckets = subparsers.add_parser(
        "buckets",
        aliases=["list_buckets"],
        description="Print a list of the S3 buckets set for this account, mapped "
        "to each alias. Any buckets that are not used by this account "
        "in this location are listed as None.",
        help="Print a list of the S3 buckets activated for this account.",
        add_help=True,
    )

    ##############################################################
    #################### 'metadata' parser #######################
    ##############################################################

    parser_metadata = subparsers.add_parser(
        "metadata",
        aliases=["m", "meta"],
        description="Print the user-defined metadata values for an s3 key.",
        help="Print the user-defined metadata values for an s3 key.",
        add_help=True,
    )
    parser_metadata.add_argument("key", help="The s3 key to print the metadata for.")
    add_subparser_bucket_param(parser_metadata)

    def custom_error_metadata(self, default_msg):
        extra_msg = "For more details type 'python {0} metadata --help'".format(
            os.path.basename(__file__)
        )
        print(
            f"{os.path.basename(__file__)} metadata error:",
            default_msg + "\n" + extra_msg,
        )

    parser_metadata.error = types.MethodType(custom_error_metadata, parser_metadata)

    ##############################################################
    # Parse args and return namespace
    if just_return_parser:
        return parser
    else:
        return parser.parse_args()


def s3_cli():
    args = define_and_parse_args_v2()

    # If no command was given, just print help.
    if args.command is None:
        parser = define_and_parse_args_v2(just_return_parser=True)
        parser.print_help()
        sys.exit(0)

    #####################################################
    #################### 'ls' parser ####################
    #####################################################
    elif args.command == "ls":
        # TODO: ADD SUPPORT FOR --meta AND/OR --md5 ARGS
        # If we supply just a . or /, make the key empty.
        if args.key in (".", "/"):
            args.key = ""

        # Remove s3: or s3:// prefix if it exists
        elif re.match(r"^s3:", args.key, re.IGNORECASE):
            args.key = args.key[3:]

        # Remove opening / if it exists.
        args.key = args.key.lstrip("/")

        s3m = S3Manager()
        try:
            results = s3m.listdir(
                args.key, bucket_type=args.bucket, recursive=args.recursive
            )
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(1)

        for r in results:
            if args.metadata or args.md5:
                if s3m.is_existing_s3_directory(r, bucket_type=args.bucket):
                    print(r)
                else:
                    print(r, s3m.get_metadata(r, bucket_type=args.bucket))

            else:
                print(r)

    #####################################################
    #################### 'rm' parser ####################
    #####################################################
    elif args.command == "rm":
        # If we supply just a . or /, make the key empty.
        if args.key in (".", "/"):
            args.key = ""

        # Remove s3: or s3:// prefix if it exists
        elif re.match(r"^s3:", args.key, re.IGNORECASE):
            args.key = args.key[3:]

        # Remove opening / if it exists.
        args.key = args.key.lstrip("/")

        s3m = S3Manager()
        try:
            s3m.delete(
                args.key,
                bucket_type=args.bucket,
                recursive=args.recursive,
                max_without_warning=None if args.yes else 100,
            )
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(1)

    ########################################################
    #################### 'cp/mv' parser ####################
    ########################################################
    elif args.command in ("cp", "mv"):
        k1 = args.key1
        k2 = args.key2
        if re.match(r"^s3:", k1, re.IGNORECASE):
            is_key1_s3 = True
            k1 = k1[3:].lstrip("/")
        else:
            is_key1_s3 = False

        if re.match(r"^s3:", k2, re.IGNORECASE):
            is_key2_s3 = True
            k2 = k2[3:].lstrip("/")
        else:
            is_key2_s3 = False

        s3m = S3Manager()

        # If they're both s3 keys, transfer the files between them.
        if is_key2_s3 and is_key1_s3:
            s3m.transfer(
                k1,
                k2,
                dst_bucket_type=args.bucket
                if args.dest_bucket is None
                else args.dest_bucket,
                src_bucket_type=args.bucket,
                recursive=args.recursive,
                delete_original=(args.command == "mv"),
                fail_quietly=True,
                include_metadata=True,
            )

        # If only the first file is an S3 key, we're downloading.
        elif is_key1_s3 and not is_key2_s3:
            s3m.download(
                k1,
                k2,
                bucket_type=args.bucket,
                delete_original=(args.command == "mv"),
                recursive=args.recursive,
            )

        # If only the second key is an S3 key, we're uploading.
        # If so, include the any metdata tags that are supplied via the command line.
        elif not is_key1_s3 and is_key2_s3:
            s3m.upload(
                k1,
                k2,
                bucket_type=args.bucket,
                recursive=args.recursive,
                delete_original=(args.command == "mv"),
                include_md5=args.md5,
                other_metadata=args.metadata,
            )

        # If neither key is an S3 key, it's asking for a local file transfer.
        # That can better be done with cp/mv on the os.
        else:
            raise ValueError(
                "One or more keys should refer to an S3 bucket prepended by 's3:' or 's3://'. Local"
                "file transfers can be handled by os-supported command-line utilitys like cp and mv."
            )

    ########################################################
    ### 'buckets' and 'list_buckets' parser ################
    ########################################################
    elif args.command in ("buckets", "list_buckets"):
        # Fetch the bucket data from the config file
        pretty_print_bucket_list()

    ########################################################
    ### 'metadata' and 'md5' parser ########################
    ########################################################
    elif args.command in ("metadata", "m", "meta"):
        s3m = S3Manager()

        # Remove s3: or s3:// prefix if it exists
        if re.match(r"^s3:", args.key, re.IGNORECASE):
            args.key = args.key[3:]
        # Remove opening / if it exists.
        args.key = args.key.lstrip("/")

        md = s3m.get_metadata(args.key, bucket_type=args.bucket)
        for k, v in md.items():
            print(f'"{k}": {v}')

    else:
        raise NotImplementedError("Command '{args.command}' not yet implemented.")


if __name__ == "__main__":
    s3_cli()
