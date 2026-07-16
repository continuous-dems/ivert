import argparse
import glob
import os
import sys

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.client_job_upload as client_job_upload
    # import ivert.s3 as s3
else:
    # If running as a script, import this way.
    import client_job_upload
    # import s3


def run_update_command(args: argparse.Namespace) -> None:
    """Run an update command from the ivert_client to the ivert_server."""
    assert hasattr(args, "polygon_file")
    assert hasattr(args, "start_date")
    assert hasattr(args, "end_date")
    assert hasattr(args, "skip_bad_granule_checks")
    assert hasattr(args, "leave_old_data")
    assert hasattr(args, "wait")

    # Make a copy we can modify to generate a Config file for the job.
    args_to_send = argparse.Namespace(**vars(args))

    if os.path.splitext(args.polygon_file)[1].lower() == ".shp":
        files_to_send = glob.glob(os.path.splitext(args.polygon_file)[0] + ".*")
    elif os.path.exists(args.polygon_file):
        files_to_send = [args.polygon_file]
    else:
        files_to_send = []

    args_to_send.files = files_to_send

    del args_to_send.wait
    # TODO: Implement waiting until the job is done.

    client_job_upload.upload_new_job(args_to_send, verbose=True)
