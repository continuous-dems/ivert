import argparse
import glob
import os
import sys
import time

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.client_job_upload as client_job_upload
    import ivert.client_job_status as client_job_status
    import ivert.jobs_database as jobs_database
    import ivert.s3 as s3
    from ivert_utils.bcolors import bcolors as bcolors
else:
    # If running as a script, import this way.
    import client_job_upload
    import client_job_status
    import jobs_database
    from utils.bcolors import bcolors as bcolors
    import s3


def run_import_command(args: argparse.Namespace) -> None:
    """Run an import command from the ivert_client to the ivert_server."""

    # Make a copy we can modify to generate a Config file for the job.
    args_to_send = argparse.Namespace(**vars(args))

    # Run through the files, populate any glob patterns.
    files_to_send = []
    for fn in args.files_or_directory:
        if os.path.isdir(fn):
            files_to_send.extend(glob.glob(os.path.join(fn, "*.tif")))
        elif s3.S3Manager.contains_glob_flags(fn):
            files_to_send.extend(glob.glob(fn))
        else:
            if not os.path.exists(fn):
                raise FileNotFoundError(f"File not found: {fn}")
            files_to_send.append(fn)

    if len(files_to_send) == 0:
        raise FileNotFoundError(f"File not found: {fn}")

    # Read lists from any text files if "read_as_list" is enabled.from
    if args.read_textfiles:
        files_to_send_2 = []
        for fn in files_to_send:
            if os.path.splitext(fn)[-1].lower() == ".txt":
                with open(fn, "r") as f:
                    files_to_send_2.extend(
                        [fn.strip() for fn in f.readlines() if len(fn.strip()) > 0]
                    )
            else:
                files_to_send_2.append(fn)

        files_to_send = files_to_send_2
        del files_to_send_2

    if len(files_to_send) == 0:
        print("No files identified to upload. Check your prompt.")
        return

    start_n = args.start_n
    if start_n > 0:
        print(f"Skipping the first {start_n} files.")
        files_to_send = files_to_send[start_n:]

    # Strip off client-only arguments
    del args_to_send.files_or_directory
    del args_to_send.prompt
    del args_to_send.read_textfiles
    del args_to_send.max_files_per_chunk
    del args_to_send.max_gb_per_chunk
    del args_to_send.start_n

    # NOW, if we've hit either of the maximums (size, or number of files), we need to divvy this up into chunks.

    total_size_gb = sum([os.path.getsize(fn) for fn in files_to_send]) / (1024**3)

    # If this job is too big, split it into separate chunks.
    if ((args.max_gb_per_chunk > 0) and (total_size_gb > args.max_gb_per_chunk)) or (
        (args.max_files_per_chunk > 0)
        and (len(files_to_send) > args.max_files_per_chunk)
    ):
        print(
            "Your job is larger than the maximum allowed. Splitting it up into chunks."
        )

        # Split up the job into chunks
        list_of_file_chunks = split_job_into_chunks(
            files_to_send, args.max_files_per_chunk, args.max_gb_per_chunk
        )
        db = jobs_database.JobsDatabaseClient()

        # For each chunk, upload it
        chunk_statuses = []
        total_files_processed = 0
        for chunk_list in list_of_file_chunks:
            args_to_send.files = chunk_list
            job_name = client_job_upload.upload_new_job(args_to_send)

            prev_job_status = None
            while True:
                cur_job_status = client_job_status.get_simple_job_status(job_name, db)

                if cur_job_status != prev_job_status:
                    print(f"{cur_job_status}", end="", flush=True)
                    prev_job_status = cur_job_status

                if cur_job_status in ("complete", "error", "killed", "unknown"):
                    chunk_statuses.append(cur_job_status)
                    break

                time.sleep(3)
                print(".", end="", flush=True)

            total_files_processed += len(chunk_list)
            print(
                f"\n{(total_files_processed + start_n):,} of {(len(files_to_send) + start_n):,} files processed."
            )

        print(
            len(chunk_statuses),
            "jobs finished importing with statuses:",
            str(chunk_statuses).lstrip("[").rstrip("]"),
        )

    else:
        # Append the "files" argument to the args_to_send object
        args_to_send.files = files_to_send

        # Upload the job
        client_job_upload.upload_new_job(args_to_send)

        print(
            f"Job uploaded. Use '{bcolors.BOLD}ivert status{bcolors.ENDC}' to check the status."
        )


def split_job_into_chunks(
    files_to_send: list[str], max_files_per_chunk: int, max_gb_per_chunk: float
) -> list[list[str]]:
    """Split a list of files into chunks.

    Args:
        files_to_send: A list of files to split up.
        max_files_per_chunk: The maximum number of files per chunk.
        max_gb_per_chunk: The maximum size of the job in GB.

    Returns:
        A list of lists of files.
    """

    chunks = []
    cur_chunk = []
    cur_chunk_size_gb = 0

    for fn in files_to_send:
        fn_size_gb = os.path.getsize(fn) / (1024**3)

        if ((len(cur_chunk) + 1) <= max_files_per_chunk) and (
            (cur_chunk_size_gb + fn_size_gb) <= max_gb_per_chunk
        ):
            cur_chunk.append(fn)
            cur_chunk_size_gb += fn_size_gb
        else:
            chunks.append(cur_chunk)
            cur_chunk = [fn]
            cur_chunk_size_gb = fn_size_gb

    chunks.append(cur_chunk)

    # Sanity check to make sure we accounted for all the files and didn't accidenally omit any
    assert sum([len(c) for c in chunks]) == len(files_to_send)

    return chunks
