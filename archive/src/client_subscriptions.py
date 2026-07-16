"""Methods for pushing subscribe and unsubscribe commands to the IVERT server."""

import argparse
import sys

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.client_job_upload as client_job_upload
    from ivert_utils.bcolors import bcolors
else:
    import client_job_upload
    from utils.bcolors import bcolors


def run_subscribe_command(args: argparse.Namespace) -> None:
    """From the ivert_client CLI, push a subscribe command to the IVERT server.

    Args:
        args (argparse.Namespace): The parsed arguments from the ivert_client CLI.
    """
    # First, set up the arguments
    args_for_server = argparse.Namespace(
        **vars(args)
    )  # Create a copy of the original arguments
    args_for_server.command = "update"
    args_for_server.sub_command = "subscribe"

    if args.username is None:
        args_for_server.username = args.email.split("@")[0]

    client_job_upload.upload_new_job(args_for_server)

    print("\nYour subscribe request has been sent to the IVERT server.")
    print(
        "You should receive an email shortly from Amazon Nofications to confirm your subscription."
    )
    return


def run_unsubscribe_command(args: argparse.Namespace) -> None:
    """From the ivert_client CLI, push an unsubscribe command to the IVERT server.

    Args:
        args (argparse.Namespace): The parsed arguments from the ivert_client CLI.
    """  # First, set up the arguments
    args_for_server = argparse.Namespace(
        **vars(args)
    )  # Create a copy of the original arguments
    args_for_server.command = "update"
    args_for_server.sub_command = "unsubscribe"

    args_for_server.username = args.email.split("@")[0]

    client_job_upload.upload_new_job(args_for_server)

    print("\nYour unsubscribe request has been sent to the IVERT server.")
    print(
        f"You will {bcolors.BOLD}not{bcolors.ENDC} get a notification back when the job is done (that's kind of the nature of this request, right?)."
    )
    print("You may resubscribe at any time using the 'src subscribe <email>' command.")
    return
