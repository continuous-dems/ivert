#!/usr/bin/env python
"""Code for setting up a new IVERT user on the client side (local machine, external to the IVERT cloud)."""

import argparse
import boto3
import os
import re
import sys
import shutil
import typing

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.client_job_upload as client_job_upload
    from ivert_utils.bcolors import bcolors
    import ivert_utils.configfile as configfile
    import ivert_utils.fetch_text as fetch_text
else:
    # If running as a script, import this way.
    import client_job_upload
    from utils.bcolors import bcolors
    import utils.configfile as configfile
    import utils.fetch_text as fetch_text

ivert_config = None
ivert_user_config_template = None


def setup_new_user(args: argparse.Namespace) -> None:
    """Set up a new IVERT user on the local machine."""

    # First, collect user inputs for any options not provided.
    args = collect_inputs(args, only_if_not_provided=True)

    # Validate all the inputs for basic correctness.
    validate_inputs(args)

    # Confirm the inputs with the user (if requested).
    if args.prompt:
        confirm_inputs_with_user(args)

    # Create the ivert local directories (in ~/.ivert)
    create_local_dirs()

    # Update the AWS profiles on the local machine.
    update_local_aws_profiles(args)

    # Update the IVERT user Config file.
    update_ivert_user_config(args)

    # Update the IVERT Config file, since the credentials fields should now be populated. They weren't before this.
    global ivert_config
    ivert_config = configfile.Config()

    # Gotta do this in the client_job_upload module too, or else these new variables won't be there either.
    # This is a bit of a hack but it works.
    client_job_upload.reset_ivert_config()
    # And the s3 module.

    if args.subscribe_to_sns:
        print(
            "\nSending a job to the IVERT server to subscribe you to IVERT SNS notifications."
        )
        # Send new_user Config (as an "update" command) to the IVERT cloud tool. This will subscribe the user to the IVERT SNS topic.
        subscribe_user_to_sns_notifications(args)

    print("\nIVERT user setup complete!")
    if args.subscribe_to_sns:
        print(
            f"\nYou sould soon receive an email from the IVERT server to {bcolors.BOLD}{args.email}{bcolors.ENDC} confirming your subscription to the IVERT SNS notifications."
        )
        print("\nAfter receiveing that email, you may ", end="")
    else:
        print("\nYou may now ", end="")

    print(
        f"run\n> {bcolors.BOLD}{bcolors.OKBLUE}ivert test{bcolors.ENDC}{bcolors.ENDC}\n...to perform a dry run end-to-end test the IVERT system.\n"
    )
    print(
        f"At any time, run\n> {bcolors.BOLD}{bcolors.OKBLUE}ivert --help{bcolors.ENDC}{bcolors.ENDC}\n...to see a complete list of other IVERT commands. "
        f"Happy Validations!\n"
    )


def read_ivert_s3_credentials(creds_file: str = "", error_if_not_found: bool = True):
    """Read the IVERT S3 credentials file.

    If we're given a path, move it first into the default credentials location."""
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config(ignore_errors=True)

    if os.path.exists(creds_file) and (
        os.path.normcase(os.path.realpath(creds_file))
        != os.path.normcase(os.path.realpath(ivert_config.ivert_s3_credentials_file))
    ):
        # Move the old creds file to the new location
        print(
            "Moving",
            os.path.basename(creds_file),
            "to",
            ivert_config.ivert_s3_credentials_file,
        )
        # Create the directory if it doesn't exist yet.
        if not os.path.exists(os.path.dirname(ivert_config.ivert_s3_credentials_file)):
            os.makedirs(os.path.dirname(ivert_config.ivert_s3_credentials_file))

        shutil.move(creds_file, ivert_config.ivert_s3_credentials_file)

    if os.path.exists(ivert_config.ivert_s3_credentials_file):
        return configfile.Config(ivert_config.ivert_s3_credentials_file)
    else:
        if error_if_not_found:
            raise FileNotFoundError(
                f"IVERT S3 credentials file '{ivert_config.ivert_s3_credentials_file}' not found."
            )
        else:
            return None


def read_ivert_personal_credentials(
    pcreds_file: str = "", error_if_not_found: bool = True
):
    """Read the IVERT personal credentials file.

    If we're given a path, move it first into the default credentials location."""
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config(ignore_errors=True)

    if os.path.exists(pcreds_file) and (
        os.path.normcase(os.path.realpath(pcreds_file))
        != os.path.normcase(
            os.path.realpath(ivert_config.ivert_personal_credentials_file)
        )
    ):
        # Move the old creds file to the new location
        print(
            "Moving",
            os.path.basename(pcreds_file),
            "to",
            ivert_config.ivert_personal_credentials_file,
        )

        # Create the directory if it doesn't exist yet.
        if not os.path.exists(
            os.path.dirname(ivert_config.ivert_personal_credentials_file)
        ):
            os.makedirs(os.path.dirname(ivert_config.ivert_personal_credentials_file))

        shutil.move(pcreds_file, ivert_config.ivert_personal_credentials_file)

    if os.path.exists(ivert_config.ivert_personal_credentials_file):
        return open(ivert_config.ivert_personal_credentials_file, "r").read()
    else:
        if error_if_not_found:
            raise FileNotFoundError(
                f"IVERT personal S3 credentials file '{ivert_config.ivert_personal_credentials_file}' not found."
            )
        else:
            return None


def subscribe_user_to_sns_notifications(args: argparse.Namespace) -> None:
    """Send new_user Config (as an "update" command) to the IVERT cloud tool. This will subscribe the user to the IVERT SNS topic."""
    if not args.subscribe_to_sns:
        return

    # Since the IVERT import bucket doesn't have an explicit prefix for "subscribe,", we simply upload it as
    # an "update" command with an additional argument "--subscribe_to_sns"). This isn't in the ivert_client CLI but
    # we fake it here.
    # Create a copy of the original arguments
    args_copy = argparse.Namespace(**vars(args))

    args_copy.command = "update"
    args_copy.sub_command = "subscribe"

    # In the ivert "subscribe" command, the parameter goes by "all", not "filter_sns".
    args_copy.all = not args_copy.filter_sns
    if hasattr(args_copy, "filter_sns"):
        del args_copy.filter_sns
    if hasattr(args_copy, "subscribe_to_sns"):
        del args_copy.subscribe_to_sns
    if hasattr(args_copy, "creds"):
        del args_copy.creds

    # Remove references to bucket names and access keys, as well as the local AWS profile names.
    # They are for setting up the local machine with AWS S3 credentials.
    # The IVERT server doesn't need those and we don't need to transmit them publicly.
    del args_copy.untrusted_bucket_name
    del args_copy.untrusted_access_key_id
    del args_copy.untrusted_secret_access_key
    del args_copy.untrusted_endpoint_url
    del args_copy.export_client_bucket_name
    del args_copy.export_client_access_key_id
    del args_copy.export_client_secret_access_key
    del args_copy.export_client_endpoint_url
    del args_copy.export_alt_bucket_name
    del args_copy.export_alt_access_key_id
    del args_copy.export_alt_secret_access_key
    del args_copy.export_alt_endpoint_url
    del args_copy.ivert_import_profile
    del args_copy.ivert_export_client_profile
    del args_copy.ivert_export_alt_profile
    # Can delete username since that will be grabbed from the user Config file (that we just set up)
    # in the ivert_client_job_upload:upload_new_job() function
    del args_copy.username
    # The "prompt" argument is not needed for the IVERT server.
    del args_copy.prompt

    # Send the command to the IVERT cloud tool.
    client_job_upload.upload_new_job(args_copy)

    return


def collect_inputs(
    args: argparse.Namespace, only_if_not_provided: bool = True
) -> argparse.Namespace:
    """Collect user inputs and return them as a dictionary.

    Any arguments that were not provided at the command line will be collected from existing Config and credentials files."""

    # Check to make sure all the args are present here.
    assert "email" in args
    assert "creds" in args
    assert "username" in args
    assert "untrusted_bucket_name" in args
    assert "untrusted_access_key_id" in args
    assert "untrusted_secret_access_key" in args
    assert "untrusted_endpoint_url" in args
    assert "export_client_bucket_name" in args
    assert "export_client_access_key_id" in args
    assert "export_client_secret_access_key" in args
    assert "export_client_endpoint_url" in args
    assert "export_alt_bucket_name" in args
    assert "export_alt_access_key_id" in args
    assert "export_alt_secret_access_key" in args
    assert "export_alt_endpoint_url" in args
    assert "ivert_import_profile" in args
    assert "ivert_export_client_profile" in args
    assert "ivert_export_alt_profile" in args

    if not args.email.strip() or not only_if_not_provided:
        args.email = input(
            "\n" + bcolors.UNDERLINE + "Your email address" + bcolors.ENDC + ": "
        ).strip()

    # Convert email to lowercase.
    args.email = args.email.lower()

    if not args.username.strip():
        args.username = args.email.split("@")[0]

    # Make sure the username is lowercase.
    args.username = args.username.lower()

    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config()

    global ivert_user_config_template
    if not ivert_user_config_template:
        ivert_user_config_template = configfile.Config(
            ivert_config.ivert_user_config_template
        )

    # Check for valid AWS profile names (they can basically be anyting except empty strings)
    # If we weren't provided a profile name or we aren't using the defaults, prompt for them.
    if not args.ivert_import_profile.strip() or not only_if_not_provided:
        args.ivert_import_profile = (
            input(
                "\nAWS profile name to use for import "
                f"[default: {ivert_user_config_template.aws_profile_ivert_import_untrusted}]: "
            ).strip()
            or ivert_user_config_template.aws_profile_ivert_import_untrusted.strip()
        )

    if not args.ivert_export_client_profile.strip() or not only_if_not_provided:
        args.ivert_export_client_profile = (
            input(
                "\nAWS profile name to use for export "
                f"[default: {ivert_user_config_template.aws_profile_ivert_export_client}]: "
            ).strip()
            or ivert_user_config_template.aws_profile_ivert_export_client.strip()
        )

    # Strip any whitespace
    args.untrusted_bucket_name = args.untrusted_bucket_name.strip()
    args.untrusted_access_key_id = args.untrusted_access_key_id.strip()
    args.untrusted_secret_access_key = args.untrusted_secret_access_key.strip()
    args.untrusted_endpoint_url = (
        ""
        if args.untrusted_endpoint_url is None
        else args.untrusted_endpoint_url.strip()
    )
    args.export_client_bucket_name = args.export_client_bucket_name.strip()
    args.export_client_access_key_id = args.export_client_access_key_id.strip()
    args.export_client_secret_access_key = args.export_client_secret_access_key.strip()
    args.export_client_endpoint_url = (
        ""
        if args.export_client_endpoint_url is None
        else args.export_client_endpoint_url.strip()
    )
    args.export_alt_bucket_name = args.export_alt_bucket_name.strip()
    args.export_alt_access_key_id = args.export_alt_access_key_id.strip()
    args.export_alt_secret_access_key = args.export_alt_secret_access_key.strip()
    args.export_alt_endpoint_url = (
        ""
        if args.export_alt_endpoint_url is None
        else args.export_alt_endpoint_url.strip()
    )

    # Boolean flags should be strictly True/False
    args.subscribe_to_sns = bool(args.subscribe_to_sns)
    args.filter_sns = bool(args.filter_sns)

    s3_creds_obj = None

    ###############################################################################################################
    # Go through each credential field, if we don't have it from the arguments, get it from the s3_credentials file
    ###############################################################################################################

    if not args.untrusted_bucket_name or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.untrusted_bucket_name = s3_creds_obj.s3_bucket_import_untrusted

    if not args.export_client_bucket_name or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_client_bucket_name = s3_creds_obj.s3_bucket_export_client

    if not args.export_alt_bucket_name or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_alt_bucket_name = s3_creds_obj.s3_bucket_export_alt

    if not args.untrusted_access_key_id or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.untrusted_access_key_id = (
                s3_creds_obj.s3_import_untrusted_access_key_id
            )

    if not args.untrusted_secret_access_key or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.untrusted_secret_access_key = (
                s3_creds_obj.s3_import_untrusted_secret_access_key
            )

    if not args.untrusted_endpoint_url or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.untrusted_endpoint_url = s3_creds_obj.s3_import_untrusted_endpoint_url

    if not args.export_client_access_key_id or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_client_access_key_id = (
                s3_creds_obj.s3_export_client_access_key_id
            )

    if not args.export_client_secret_access_key or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_client_secret_access_key = (
                s3_creds_obj.s3_export_client_secret_access_key
            )

    if not args.export_client_endpoint_url or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_client_endpoint_url = s3_creds_obj.s3_export_client_endpoint_url

    if not args.export_alt_access_key_id or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_alt_access_key_id = s3_creds_obj.s3_export_alt_access_key_id

    if not args.export_alt_secret_access_key or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_alt_secret_access_key = (
                s3_creds_obj.s3_export_alt_secret_access_key
            )

    if not args.export_alt_endpoint_url or not only_if_not_provided:
        if not s3_creds_obj:
            s3_creds_obj = read_ivert_s3_credentials(args.creds)
        if s3_creds_obj is not None:
            args.export_alt_endpoint_url = s3_creds_obj.s3_export_alt_endpoint_url

    ##############################################################################################################
    # The credentials file may have blank fields with [USE_PERSONAL_ACCESS_KEY_ID] and
    # [USE_PERSONAL_SECRET_ACCESS_KEY] in them. If they do, swap out these values with the user's own credentials.
    ##############################################################################################################
    pcreds_text = None

    if args.untrusted_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.untrusted_access_key_id = fetch_text.fetch_access_key_id(pcreds_text)

    if args.export_client_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.export_client_access_key_id = fetch_text.fetch_access_key_id(
                pcreds_text
            )

    if args.export_alt_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.export_alt_access_key_id = fetch_text.fetch_access_key_id(pcreds_text)

    if args.untrusted_secret_access_key == "[USE_PERSONAL_SECRET_ACCESS_KEY]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.untrusted_secret_access_key = fetch_text.fetch_secret_access_key(
                pcreds_text
            )

    if args.export_client_secret_access_key == "[USE_PERSONAL_SECRET_ACCESS_KEY]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.export_client_secret_access_key = fetch_text.fetch_secret_access_key(
                pcreds_text
            )

    if args.export_alt_secret_access_key == "[USE_PERSONAL_SECRET_ACCESS_KEY]":
        if not pcreds_text or not only_if_not_provided:
            pcreds_text = read_ivert_personal_credentials(args.personal_creds)
        if pcreds_text:
            args.export_alt_secret_access_key = fetch_text.fetch_secret_access_key(
                pcreds_text
            )

    ##############################################################################################################
    # Make sure we have all the required credentials and bucket names.
    # If not, warn the user and exit.
    ##############################################################################################################
    if not (
        args.untrusted_bucket_name
        and args.export_client_bucket_name
        and args.untrusted_access_key_id
        and args.untrusted_secret_access_key
        and args.export_client_access_key_id
        and args.export_client_secret_access_key
        and args.export_alt_access_key_id
        and args.export_alt_secret_access_key
        and args.untrusted_access_key_id != "[USE_PERSONAL_ACCESS_KEY_ID]"
        and args.export_client_access_key_id != "[USE_PERSONAL_ACCESS_KEY_ID]"
        and args.untrusted_secret_access_key != "[USE_PERSONAL_SECRET_ACCESS_KEY]"
        and args.export_client_secret_access_key != "[USE_PERSONAL_SECRET_ACCESS_KEY]"
        and args.export_alt_access_key_id != "[USE_PERSONAL_ACCESS_KEY_ID]"
        and args.export_client_secret_access_key != "[USE_PERSONAL_ACCESS_KEY_ID]"
    ):
        if not args.untrusted_bucket_name:
            print("Missing untrusted bucket name.")
        if not args.export_client_bucket_name:
            print("Missing export client bucket name.")
        if (
            not args.untrusted_access_key_id
            or args.untrusted_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]"
        ):
            print("Missing untrusted bucket access key ID.")
        if (
            not args.export_client_access_key_id
            or args.export_client_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]"
        ):
            print("Missing export bucket access key ID.")
        if (
            not args.untrusted_secret_access_key
            or args.untrusted_secret_access_key == "[USE_PERSONAL_SECRET_ACCESS_KEY]"
        ):
            print("Missing untrusted bucket secret access key.")
        if (
            not args.export_client_secret_access_key
            or args.export_client_secret_access_key
            == "[USE_PERSONAL_SECRET_ACCESS_KEY]"
        ):
            print("Missing export bucket secret access key.")
        if (
            not args.export_alt_access_key_id
            or args.export_alt_access_key_id == "[USE_PERSONAL_ACCESS_KEY_ID]"
        ):
            print("Missing export-alt bucket access key ID.")
        if (
            not args.export_alt_secret_access_key
            or args.export_alt_secret_access_key == "[USE_PERSONAL_SECRET_ACCESS_KEY]"
        ):
            print("Missing export-alt bucket secret access key.")

        print(
            "Check your credentials file and/or your personal credentials file, and try again."
            "\nIf the problem persists, contact your IVERT administrator."
        )
        sys.exit(0)

    return args


def validate_inputs(args: argparse.Namespace) -> None:
    """Validate user inputs for correctness. These should be a completed set of args, not a partial set."""
    assert "email" in args
    assert "username" in args
    assert "creds" in args
    assert "untrusted_bucket_name" in args
    assert "untrusted_access_key_id" in args
    assert "untrusted_secret_access_key" in args
    assert "export_client_bucket_name" in args
    assert "export_client_access_key_id" in args
    assert "export_client_secret_access_key" in args
    assert "export_client_endpoint_url" in args
    assert "export_alt_bucket_name" in args
    assert "export_alt_access_key_id" in args
    assert "export_alt_secret_access_key" in args
    assert "export_alt_endpoint_url" in args
    assert "ivert_import_profile" in args
    assert "ivert_export_client_profile" in args
    assert "ivert_export_alt_profile" in args
    assert "subscribe_to_sns" in args
    assert "filter_sns" in args

    # Validate a correct email address.
    if not fetch_text.fetch_email_address(args.email):
        print()
        raise ValueError(f"{args.email} is an invalid email address.")

    # Check bucket names for validity.
    for bname in [
        args.untrusted_bucket_name,
        args.export_client_bucket_name,
        args.export_alt_bucket_name,
    ]:
        if not fetch_text.fetch_aws_bucketname(bname):
            print()
            raise ValueError(f"{bname} is an invalid bucket name.")

    # Check access key IDs for validity.
    for akid in [
        args.untrusted_access_key_id,
        args.export_client_access_key_id,
        args.export_alt_access_key_id,
    ]:
        if not fetch_text.fetch_access_key_id(akid):
            print()
            raise ValueError(f"{akid} is an invalid access key ID.")

    # Check secret access keys for validity.
    for sak in [
        args.untrusted_secret_access_key,
        args.export_client_secret_access_key,
        args.export_alt_secret_access_key,
    ]:
        if not fetch_text.fetch_secret_access_key(sak):
            print()
            raise ValueError(f"{sak} is an invalid secret access key.")

    return


def confirm_inputs_with_user(args: argparse.Namespace) -> None:
    """
    Prompt the user to confirm the inputs.
    """
    print()
    print(bcolors.OKGREEN + "Please confirm the following:" + bcolors.ENDC)

    headings = (
        "Email",
        "Username",
        "Untrusted bucket",
        "Untrusted access key ID",
        "Untrusted secret access key",
        "Untrusted endpoint URLExport bucket",
        "Export access key ID",
        "Export secret access key",
        "Export endpoint URLExport bucket (alt)",
        "Export access key ID (alt)",
        "Export secret access key (alt)",
        "Export endpoint URL (alt)Subscribe to email notifications",
        "Filter email notifications by user",
    )

    # Get max lengths of each field:
    max_left = max([len(k) for k in headings])
    for k, v in zip(
        headings,
        (
            args.email,
            args.user,
            args.untrusted_bucket_name,
            args.untrusted_access_key_id,
            args.untrusted_secret_access_key,
            args.untrusted_endpoint_url,
            args.export_client_bucket_name,
            args.export_client_access_key_id,
            args.export_client_secret_access_key,
            args.export_client_endpoint_url,
            args.export_alt_bucket_name,
            args.export_alt_access_key_id,
            args.export_alt_secret_access_key,
            args.export_alt_endpoint_url,
            args.subscribe_to_sns,
            args.filter_sns,
        ),
    ):
        print(f"  {(k + r': ').ljust(max_left + 2)}{v}")

    print()
    print(
        bcolors.OKGREEN
        + bcolors.BOLD
        + "Is this correct?"
        + bcolors.ENDC
        + bcolors.ENDC,
        end="",
    )
    params_confirmed = input(" [y]/n: ") or "y"

    if params_confirmed.strip()[0].lower() != "y":
        print()
        print(bcolors.FAIL + "Exiting." + bcolors.ENDC)
        sys.exit(1)

    print()
    return


def update_local_aws_profiles(args: argparse.Namespace) -> None:
    """
    Update the AWS profiles.
    """
    aws_config_file, aws_credentials_file = get_aws_config_and_credentials_files()
    update_local_aws_config(aws_config_file, args)
    update_local_aws_credentials(aws_credentials_file, args)
    return


def update_local_aws_config(aws_config_file: str, args: argparse.Namespace) -> None:
    """
    Update the AWS Config file.
    """
    # if the Config file doesn't exist, create it
    if not os.path.exists(aws_config_file):
        # Make the directory if it doesn't exist.
        if not os.path.exists(os.path.dirname(aws_config_file)):
            os.makedirs(os.path.dirname(aws_config_file))

        # Create the file (a blank version)
        with open(aws_config_file, "w") as f:
            f.write("")

    # Check that the Config file exists
    assert os.path.exists(aws_config_file)

    # Read the Config file.
    with open(aws_config_file, "r") as f:
        config_text = f.read()

    # config_text_old = config_text

    # Find eac profile if it already exists. If so, replace it. If not, add it.
    for profile_id_string, bname, endpoint_url in [
        (
            args.ivert_import_profile,
            args.untrusted_bucket_name,
            args.untrusted_endpoint_url,
        ),
        (
            args.ivert_export_client_profile,
            args.export_client_bucket_name,
            args.export_client_endpoint_url,
        ),
        (
            args.ivert_export_alt_profile,
            args.export_alt_bucket_name,
            args.export_alt_endpoint_url,
        ),
    ]:
        # Identify if old IVERT profile names are being used here.
        if (
            "[profile ivert_ingest]" in config_text
            and profile_id_string == args.ivert_import_profile
        ):
            old_ivert_profile_string = "[profile ivert_ingest]"
        elif (
            "[profile ivert_export]" in config_text
            and profile_id_string == args.ivert_export_client_profile
        ):
            old_ivert_profile_string = "[profile ivert_export]"
        else:
            old_ivert_profile_string = f"[profile {profile_id_string}]"

        new_ivert_profile = (
            "\n".join(
                [
                    f"[profile {profile_id_string}]",
                    "output = json",
                    f"region = {get_region_name_from_bucket_name(bname)}",
                ]
            )
            + (f"\nendpoint_url = {endpoint_url}" if endpoint_url else "")
            + "\n\n"
        )

        if old_ivert_profile_string in config_text:
            # Try to find the entire profile string with all options, until either the end of the file or the next profile string.
            old_ivert_profile_search_regex = (
                old_ivert_profile_string.replace("[", r"\[").replace("]", r"\]")
                + r"[\w\s\d\=\-\"\':\+/\.]*(?=(\s\[profile )|\Z)"
            )

            m = re.search(old_ivert_profile_search_regex, config_text)
        else:
            old_ivert_profile_search_regex = None
            m = None

        # If we found the profile, replace it. If not, add it.
        if m is None:
            config_text = str(config_text.rstrip("\n\r ")) + "\n\n" + new_ivert_profile
        else:
            config_text = re.sub(
                old_ivert_profile_search_regex, new_ivert_profile, config_text, count=1
            )

    # Get rid of excess newlines that may have accidentally been added in the Config file, which sometimes happens.
    config_text = config_text.rstrip("\n\r ").lstrip("\n\r ") + "\n"
    while "\n\n\n" in config_text:
        config_text = config_text.replace("\n\n\n", "\n\n")

    # Overwrite the Config file.
    with open(aws_config_file, "w") as f:
        f.write(config_text)

    print(f"Updated {aws_config_file.replace(os.environ['HOME'], '~')}.")

    return


def update_local_aws_credentials(
    aws_credentials_file: str, args: argparse.Namespace
) -> None:
    """
    Update the AWS credentials file.
    """
    # If it doesn't exist, create it.
    if not os.path.exists(aws_credentials_file):
        with open(aws_credentials_file, "w") as f:
            f.write("")

    # Check that the credentials file exists
    assert os.path.exists(aws_credentials_file)

    # Read the credentials file.
    with open(aws_credentials_file, "r") as f:
        credentials_text = f.read()

    credentials_text_old = credentials_text

    # Find IAC profile if it already exists. If so, replace it. If not, add it.
    for profile_id_string, access_key_id, secret_access_key, endpoint_url in [
        (
            args.ivert_import_profile,
            args.untrusted_access_key_id,
            args.untrusted_secret_access_key,
            args.untrusted_endpoint_url,
        ),
        (
            args.ivert_export_client_profile,
            args.export_client_access_key_id,
            args.export_client_secret_access_key,
            args.export_client_endpoint_url,
        ),
        (
            args.ivert_export_alt_profile,
            args.export_alt_access_key_id,
            args.export_alt_secret_access_key,
            args.export_alt_endpoint_url,
        ),
    ]:
        new_ivert_profile_string = f"[{profile_id_string}]"

        # Old IVERT versions (<0.5.0) used "ivert_ingest" instead of "ivert_import_untrusted"
        # and "ivert_export" instead of "ivert_export_untrusted". Look for those.
        if (
            "[ivert_ingest]" in credentials_text
            and profile_id_string == args.ivert_import_profile
        ):
            old_ivert_profile_string = "[ivert_ingest]"
        elif (
            "[ivert_export]" in credentials_text
            and profile_id_string == args.ivert_export_client_profile
        ):
            old_ivert_profile_string = "[ivert_export]"
        else:
            old_ivert_profile_string = new_ivert_profile_string

        # Create a new profile string for that profile.
        new_ivert_profile = (
            "\n".join(
                [
                    new_ivert_profile_string,
                    f"aws_access_key_id = {access_key_id}",
                    f"aws_secret_access_key = {secret_access_key}",
                ]
            )
            + (f"\nendpoint_url = {endpoint_url}" if endpoint_url else "")
            + "\n\n"
        )

        # Create a search regex for the complete text of the previous profile.
        if old_ivert_profile_string in credentials_text:
            # Try to find the entire profile string with all options, until either the end of the file or the next
            # profile string.
            old_ivert_profile_search_regex = (
                old_ivert_profile_string.replace("[", r"\[").replace("]", r"\]")
                + r"[\w\s\d\=\-\"\'\+/:\.]*(?=(\s\[)|\Z)"
            )

            m = re.search(old_ivert_profile_search_regex, credentials_text)
        else:
            old_ivert_profile_search_regex = None
            m = None

        # If we found the profile, replace it. If not, add it.
        if m is None:
            credentials_text = (
                str(credentials_text.rstrip()) + "\n\n" + new_ivert_profile
            )
        else:
            credentials_text = re.sub(
                old_ivert_profile_search_regex,
                new_ivert_profile,
                credentials_text,
                count=1,
            )

    # Get rid of excess newlines that may have accidentally been added in the Config file.
    credentials_text = credentials_text.rstrip("\n\r ").lstrip("\n\r ") + "\n"
    while "\n\n\n" in credentials_text:
        credentials_text = credentials_text.replace("\n\n\n", "\n\n")

    # Overwrite the credentials file.
    with open(aws_credentials_file, "w") as f:
        f.write(credentials_text)

    print(f"Updated {aws_credentials_file.replace(os.environ['HOME'], '~')}.")

    return


def create_local_dirs() -> None:
    """Create the local directories needed to store IVERT user data."""
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config(ignore_errors=True)

    # creds_folder = ivert_config.user_data_creds_directory
    jobs_folder = ivert_config.ivert_jobs_directory_local

    # if not os.path.exists(creds_folder):
    #     os.makedirs(creds_folder)
    if not os.path.exists(jobs_folder):
        os.makedirs(jobs_folder)

    return


def update_ivert_user_config(args: argparse.Namespace) -> None:
    """Create or overwrite the ivert_user_config_[name].ini file."""
    # First, find all instances of existing user Config files in the Config/ directory.
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config(ignore_errors=True)

    user_config_file = ivert_config.user_configfile

    # Get the text from the user Config template.
    with open(ivert_config.ivert_user_config_template, "r") as f:
        user_config_text = f.read()

    # Update the user Config text with the new values.
    # user_config_text = re.sub(r"user_email\s*[=]\s*[\w\[\].@-]+", f"user_email = {args.email}", user_config_text)

    # Update the username in the user Config text.
    # user_config_text = re.sub(r"username\s*[=]\s*[\w\[\].-]+", f"username = {args.username}", user_config_text)

    # In previous IVERT versions (<0.5.0) these were called "aws_profile_ivert_ingest" and "aws_profile_ivert_export".
    # If those exist in the user Config, change them to the new field names here.
    # user_config_text = re.sub(r"aws_profile_ivert_ingest\s*=\s*",
    #                           r'aws_profile_ivert_import_untrusted = ',
    #                           user_config_text)
    # user_config_text = re.sub(r"aws_profile_ivert_export\s*=\s*",
    #                           r'aws_profile_ivert_export_client = ',
    #                           user_config_text)

    # Update the aws_profile_ivert_import_untrusted in the user Config text, if needed.
    # If it's using a different profile name, then update it.
    user_config_text = re.sub(
        r"aws_profile_ivert_import_untrusted\s*[=]\s*[\w\[\].-]+",
        f"aws_profile_ivert_import_untrusted = {args.ivert_import_profile}",
        user_config_text,
    )
    user_config_text = re.sub(
        r"aws_profile_ivert_export_client\s*[=]\s*[\w\[\].-]+",
        f"aws_profile_ivert_export_client = {args.ivert_export_client_profile}",
        user_config_text,
    )
    user_config_text = re.sub(
        r"aws_profile_ivert_export_alt\s*[=]\s*[\w\[\].-]+",
        f"aws_profile_ivert_export_alt = {args.ivert_export_alt_profile}",
        user_config_text,
    )

    # Write the boolean flags for the user Config file. Overwrite any old ones.
    user_config_text = re.sub(
        r"subscribe_to_sns\s*[=]\s*[\w\[\].-]+",
        f"subscribe_to_sns = {str(args.subscribe_to_sns)}",
        user_config_text,
    )
    user_config_text = re.sub(
        r"filter_sns_by_username\s*[=]\s*[\w\[\].-]+",
        f"filter_sns_by_username = {str(args.filter_sns)}",
        user_config_text,
    )

    # Write the user Config file. Overwrite any old one.
    with open(user_config_file, "w") as f:
        f.write(user_config_text)

    print(f"{user_config_file} written.")

    return


def get_aws_config_and_credentials_files() -> typing.List[str]:
    """Find the locations of the AWS Config and credentials files.

    If the locations are set in the environment variables, use those.
    Otherwise, use the default locations as specified in the AWS documentation:
    https://docs.aws.amazon.com/sdkref/latest/guide/file-location.html

    The default locations are in the user's home directory:
    ~/.aws/Config
    ~/.aws/credentials

    Create the Config and credentials files if they don't exist.

    Returns
    -------
    list[str]
        The filenames of the Config and credentials files.
    """
    # First, check to see if these locations are set in the environment variables.
    # Documentation from https://docs.aws.amazon.com/sdkref/latest/guide/file-location.html
    config_file = os.environ.get("AWS_CONFIG_FILE")
    credentials_file = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")

    # If they aren't in the user environment variables, then use the default locations.
    if config_file is None:
        config_file = os.path.expanduser("~/.aws/config")
    if credentials_file is None:
        credentials_file = os.path.expanduser("~/.aws/credentials")

    return [config_file, credentials_file]


def get_region_name_from_bucket_name(bucket_name: str) -> str:
    """Get the region name from a bucket name. The NCCF bucket names include the region name in them.

    The available region names can be found at:
    https://docs.aws.amazon.com/general/latest/gr/rande.html

    If the region name is not found in the bucket name, default to "us-east-1".

    Parameters
    ----------
    bucket_name : str
        The name of the bucket.

    Returns
    -------
    str
        The region name for the bucket.
    """
    region_names = boto3.Session().get_available_regions("s3")
    for region_name in region_names:
        if region_name in bucket_name:
            return region_name

    # If we get here, we couldn't find a region name inside the bucket name.
    # Default to us-east-1
    return "us-east-1"


def define_and_parse_args(
    just_return_parser: bool = False, ignore_config_errors: bool = False
):
    """Define and parse command-line arguments."""

    global ivert_config
    global ivert_user_config_template

    if not ivert_config:
        ivert_config = configfile.Config(ignore_errors=ignore_config_errors)
    if not ivert_user_config_template:
        ivert_user_config_template = configfile.Config(
            ivert_config.ivert_user_config_template
        )

    parser = argparse.ArgumentParser(
        description="Change IVERT default settings on the local machine."
    )
    # TODO: Add options for setting local variables to your IVERT jobs and default directories.

    # No longer operating in "online" mode. Just offline (for now)
    # parser.add_argument("--offline", type=bool, required=False, default=True, action="store_true",
    #                     help="Set up IVERT to run in 'offline' mode on the local machine (Default). "
    #                         "This is ignored if '--online' is set.")
    # parser.add_argument("--online", type=bool, required=False, default=False, action="store_true",
    #                     help="Set up IVERT to run in 'online' mode in the cloud. "
    #                          "This requires a username and credentials to be provided. If you've never set up IVERT "
    #                          "on this machine before, use the flags below. If you already have, your previous "
    #                          "configuration files will be reused automatically. If set, this supersedes the 'offline' setting.")

    # Online group, for settings that are used only when setting up in 'online' mode.

    # online_group = parser.add_argument_group("IVERT online setup prompts.",
    #                                          description="If '--online' is chosen above and you're setting up for the "
    #                                                      "first time on this machine, use the following prompts to "
    #                                                      "connect your IVERT installation to the cloud. These are "
    #                                                      "ignored if setup is run in 'offline' mode.")
    #
    # online_group.add_argument("email", type=is_email.return_email, required=False, default="",
    #                     help="The email address of the user (unneeded if running in offline mode, or if the email is already in the user's IVERT user_config.ini file.")
    # online_group.add_argument("-u", "--username", dest="username", type=str, required=False, default="",
    #                     help="The username of the new user. Only needed if you want to create a custom username. "
    #                          "Default: Derived from the left side of your email. It's recommended you just "
    #                          "use the default unless you have specific reasons to do otherwise.")
    # online_group.add_argument("-c", "--creds", dest="creds", type=str, required=False, default="",
    #                     help="The path to the 'ivert_s3_credentials.ini' file. "
    #                          "This file will be moved to your ~/.ivert/creds directory.")
    # online_group.add_argument("-pc", "--personal_creds", dest="personal_creds", type=str, required=False, default="",
    #                     help="The path to the your personal credentials file for the export bucket. "
    #                          "Should contain an AWS access key ID and a secret access key.")
    # # Note, this option is an "opt-out", but gets saved as a positive "opt-in" in the variable.
    # online_group.add_argument("-ns", "--do_not_subscribe", dest="subscribe_to_sns", action="store_false",
    #                     default=True, required=False,
    #                     help="Do not subscribe the new user to the IVERT SNS topic to receive email notifications. "
    #                          "Default: Subscribe to email notifications. (If you were already subscribed with the "
    #                          "same email, the subscription settings will just be overwritten. You will not be "
    #                          "'double-subscribed'.)")
    # # Note, this option is an "opt-out", but gets saved as a positive "opt-in" in the variable.
    # online_group.add_argument("-nf", "--no_sns_filter", dest="filter_sns", action="store_false",
    #                     default=True, required=False,
    #                     help="Do not filter email notifications by username. "
    #                          "This will make you receive all emails from all jobs. "
    #                          "Does nothing if the -ns flag is used. "
    #                          "Default: Only get emails for jobs that *this* user submits.")
    # online_group.add_argument("-p", "--prompt", dest="prompt", default=False, action="store_true",
    #                     help="Prompt the user to verify settings before setting up IVERT. Use if you'd like to have "
    #                          "'one last look' before the setup scripts complete. (If something was wrong, you can "
    #                          "always re-run this command and it will overwrite old settings. Default: False (no prompt)")

    # Bucket group, for settings that are provided only if the user wants to manually feed bucket credentials to the tool
    # rather than using a credentials INI file.
    # bucket_group = parser.add_argument_group("IVERT S3 bucket settings",
    #                                          description="Manually enter the IVERT S3 bucket settings and credentials if"
    #                                                      " setting up in 'online' mode. "
    #                                                      "It is FAR EASIER to copy the 'ivert_s3_credentials.ini' file "
    #                                                      "from the team's GDrive and use the --creds flag above. "
    #                                                      "The script will automatically pull these variables from there. "
    #                                                      "But if you wanna do it manually, whatevs.")
    # bucket_group.add_argument("-ub", "--untrusted_bucket_name", dest="untrusted_bucket_name",
    #                           default="", type=str, required=False,
    #                           help="The name of the bucket where untrusted data uploaded to IVERT.")
    # bucket_group.add_argument("-uak", "--untrusted_access_key_id", dest="untrusted_access_key_id",
    #                           default="", type=str, required=False,
    #                           help="The access key ID for the bucket where untrusted data uploaded to IVERT.")
    # bucket_group.add_argument("-usk", "--untrusted_secret_access_key", dest="untrusted_secret_access_key",
    #                           default="", type=str, required=False,
    #                           help="The secret access key for the bucket where untrusted data uploaded to IVERT.")
    # bucket_group.add_argument("-ueu", "--untrusted_endpoint_url", dest="untrusted_endpoint_url",
    #                           default="", type=str, required=False,
    #                           help="The endpoint URL for the bucket where import data is uploaded to IVERT. "
    #                                "Default: None")
    # bucket_group.add_argument("-xb", "--export_client_bucket_name", dest="export_client_bucket_name",
    #                           default="", type=str, required=False,
    #                           help="The name of the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xak", "--export_client_access_key_id", dest="export_client_access_key_id",
    #                           default="", type=str, required=False,
    #                           help="The access key ID for the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xsk", "--export_client_secret_access_key", dest="export_client_secret_access_key",
    #                           default="", type=str, required=False,
    #                           help="The secret access key for the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xeu", "--export_client_endpoint_url", dest="export_client_endpoint_url",
    #                           default="", type=str, required=False,
    #                           help="The endpoint URL for the bucket where exported data downloaded from IVERT. "
    #                                "Default: None")
    # bucket_group.add_argument("-xab", "--export_alt_bucket_name", dest="export_alt_bucket_name",
    #                           default="", type=str, required=False,
    #                           help="The name of the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xaak", "--export_alt_access_key_id", dest="export_alt_access_key_id",
    #                           default="", type=str, required=False,
    #                           help="The access key ID for the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xask", "--export_alt_secret_access_key", dest="export_alt_secret_access_key",
    #                           default="", type=str, required=False,
    #                           help="The secret access key for the bucket where IVERT data is exported to be downloaded.")
    # bucket_group.add_argument("-xaeu", "--export_alt_endpoint_url", dest="export_alt_endpoint_url",
    #                           default="", type=str, required=False,
    #                           help="The endpoint URL for the bucket where exported data downloaded from IVERT. "
    #                                "Default: None")
    #
    # aws_group = parser.add_argument_group("IVERT AWS profile settings",
    #                                       description="Manually enter the IVERT AWS profile names. Only useful if "
    #                                                   "either of these names are already used and you want to avoid "
    #                                                   "conflicts. It's recommended to just use the default settings "
    #                                                   "here and skip these options.")
    #
    # aws_group.add_argument("-ip", "--ivert_import_profile", dest="ivert_import_profile",
    #                        default=ivert_user_config_template.aws_profile_ivert_import_untrusted,
    #                        type=str, required=False,
    #                        help="Manually set the name of the AWS profile for IVERT import. "
    #                             f" Default: '{ivert_user_config_template.aws_profile_ivert_import_untrusted}'.")
    #
    # aws_group.add_argument("-xp", "--ivert_export_client_profile", dest="ivert_export_client_profile",
    #                        default=ivert_user_config_template.aws_profile_ivert_export_client,
    #                        type=str, required=False,
    #                        help="Manually set the name of the AWS profile for IVERT export. "
    #                             f"Default: '{ivert_user_config_template.aws_profile_ivert_export_client}'.")
    #
    # aws_group.add_argument("-xap", "--ivert_export_alt_profile", dest="ivert_export_alt_profile",
    #                        default=ivert_user_config_template.aws_profile_ivert_export_alt,
    #                        type=str, required=False,
    #                        help="Manually set the name of the AWS profile for IVERT export-alt. "
    #                             f"Default: '{ivert_user_config_template.aws_profile_ivert_export_alt}'.")

    if just_return_parser:
        return parser
    else:
        return parser.parse_args()


if __name__ == "__main__":
    input_args = define_and_parse_args(ignore_config_errors=True)

    # Just for local testing. Normally this is run as a subset menu of client.py, not standalone. But it can be run standalone.
    setup_new_user(input_args)
