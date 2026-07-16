"""Code for pre-processing and submitting a validate command from the ivert_client to the ivert_server."""

import argparse
import glob
import os
import sys
import time

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.client_job_download as client_job_download
    import ivert.client_job_upload as client_job_upload
    import ivert.client_job_status as client_job_status
    import ivert.s3 as s3
    import ivert_utils.query_yes_no as yes_no
    from ivert_utils.bcolors import bcolors as bcolors
    import ivert_utils.configfile as configfile
else:
    # If running as a script, import this way.
    import client_job_download
    import client_job_upload
    import client_job_status
    import s3
    import utils.query_yes_no as yes_no
    from utils.bcolors import bcolors as bcolors
    import utils.configfile as configfile

ivert_config = None


def run_validate_command(args: argparse.Namespace) -> None:
    """Run a validate command from the ivert_client to the ivert_server."""
    assert hasattr(args, "command") and args.command == "validate"
    assert hasattr(args, "files_or_directory")
    assert hasattr(args, "input_vdatum")
    assert hasattr(args, "region_name")
    assert hasattr(args, "wait")
    assert hasattr(args, "prompt")
    assert hasattr(args, "measure_coverage")
    assert hasattr(args, "include_photons")
    assert hasattr(args, "band_num")
    assert hasattr(args, "coastlines_only")
    assert hasattr(args, "mask_osm_buildings")
    assert hasattr(args, "mask_bing_buildings")
    assert hasattr(args, "mask_wsf_urban")
    assert hasattr(args, "outlier_sd_threshold")

    global ivert_config
    if ivert_config is None:
        ivert_config = configfile.Config()

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

    # Create absolute paths.
    files_to_send = [os.path.abspath(fn) for fn in files_to_send]

    # If there are no files to send, raise an error. Could happen if the user had a mistake in a glob pattern that
    # doesn't match any files.
    if len(files_to_send) == 0:
        raise ValueError(
            f"{args.files_or_directory} has no matching files to validate."
        )

    # Convert to absolute paths.
    files_to_send = [os.path.abspath(fn) for fn in files_to_send]

    # Replace the 'files_or_directory' with the 'files' argument.
    del args_to_send.files_or_directory
    args_to_send.files = files_to_send
    # The 'wait' and 'prompt' flags are server-side only.
    del args_to_send.prompt
    del args_to_send.wait

    # Prompt the user if they've asked.
    if args.prompt:
        print(client_job_upload.convert_cmd_args_to_string(args_to_send))
        answer = yes_no.query_yes_no("Proceed?", default="yes")
        # If they don't want to proceed, just exit.
        if not answer:
            sys.exit(0)

    # Upload the job.
    client_job_upload.upload_new_job(args_to_send, verbose=True)

    # If asked, wait for the job to finished.
    if args.wait:
        print("Waiting for job to complete...")
        # Find the name of the job we just submitted, from the local directories.
        job_name = client_job_status.get_latest_job_name_from_local_dirs()
        ivert_job_status = None
        start_time = time.time()

        while ivert_job_status not in ("complete", "error", "killed", "unknown"):
            new_job_status = client_job_status.get_simple_job_status(job_name)
            if new_job_status != ivert_job_status:
                print(f"'{new_job_status}'", end="")

            ivert_job_status = new_job_status

            time.sleep(5)
            print(".", end="")

            minutes_since_start = (time.time - start_time) / 60.0
            if (
                minutes_since_start
                >= ivert_config.ivert_server_job_file_download_timeout_mins
            ):
                print(
                    f"Job timed out. Exiting. Run '{bcolors.BOLD}ivert status{bcolors.ENDC}' to see if/when it finishes. If it doesn't, you may want to contact your IVERT administrator to see if the IVERT server is still running.."
                )
                sys.exit(0)

        print("Downloading results.")
        client_job_download.download_job(job_name, dest=".")

    else:
        print(
            f"Job submitted.\n"
            f"Run '{bcolors.BOLD}ivert status{bcolors.ENDC}' to check the status of the job, or '{bcolors.BOLD}ivert status -d{bcolors.ENDC}' to provide more detail."
        )
