"""Code for uploading new jobs to the S3 bucket."""

import argparse
import datetime
import glob
import os
import string
import sys

if vars(sys.modules[__name__])["__package__"] == "ivert":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert.jobs_database as jobs_database
    import ivert.s3 as s3
    import ivert.client_job_status as client_job_status
    import ivert_utils.configfile as configfile
    import ivert_utils.progress_bar as progress_bar
    import ivert_utils.version as version
else:
    import jobs_database
    import s3
    import client_job_status
    import utils.configfile as configfile
    import utils.progress_bar as progress_bar
    import utils.version as version

# The ivert_config object loads user information from the user's Config file if it exists.
ivert_config = None


def reset_ivert_config():
    # Reset the ivert_config object(s). Useful during a new client install, when we first start up IVERT but it
    # hasn't yet transferred the user credentials file, it doesn't have all the fields populated. This resets those.
    # This is called from the parent object.
    global ivert_config
    ivert_config = configfile.Config()
    s3.ivert_config = configfile.Config()
    client_job_status.ivert_config = configfile.Config()


def convert_cmd_args_to_string(args):
    "Convert the command arguments to a string for the purpose of sending a message to the user."
    assert hasattr(args, "command")
    assert hasattr(args, "files") and isinstance(args.files, list)
    command_str = args.command
    for key, val in vars(args).items():
        if key in ("command", "files"):
            continue
        command_str = command_str + f" --{key} {repr(val)}"
    if len(args.files) > 0:
        command_str = command_str + " --files"
    for fname in args.files:
        if len(fname) == 0:
            continue
        command_str = (
            command_str + " " + (f'"{fname}"' if contains_whitespace(fname) else fname)
        )

    return command_str


def contains_whitespace(s):
    """Returns T/F if a string has any whitespace in it."""
    return any([wch in s for wch in string.whitespace])


def create_new_job_params(username: str = None) -> tuple[str, int]:
    """Create a new job number and username pair.

    Args:
        username (str, optional): The username of the user. Defaults to just getting it from the Config file.

    Returns:
        str: Username
        int: New job number
    """
    # Check the last job submitted, either from the user's local files or from the jobs database.
    db = jobs_database.JobsDatabaseClient()

    last_job_name = client_job_status.find_latest_job_submitted(username, jobs_db=db)
    if last_job_name is None:
        last_job_name = "nada_000000000000"

    #############################################################################################3
    # TODO: Get the new job number from the REST API, not the database.
    last_job_number = int(last_job_name[-12:])

    last_job_nubmer_by_anyone = db.fetch_latest_job_number_from_s3_metadata()
    if last_job_nubmer_by_anyone is not None:
        last_job_number = max(last_job_nubmer_by_anyone, last_job_number)

    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config()

    # Get the username.
    if not username:
        username = ivert_config.username

    if not username:
        raise ValueError(f"Username not defined in {ivert_config.user_config_file}.")

    # The last job number is YYYYMMDDNNNN. If the last job was "today", we just increment it. Otherwise, we create a new job number using today's date.
    today_str = datetime.date.today().strftime("%Y%m%d")
    last_job_date_str = str(last_job_number)[:8]

    # Create a new job number. If the last job was "today", we just increment it to the next job number from today.
    # Otherwise, we create a new job number using today's date.
    if today_str == last_job_date_str:
        new_job_number = last_job_number + 1
    else:
        new_job_number = int(datetime.date.today().strftime("%Y%m%d")) * 10000
    ##################################################################################################

    return username, new_job_number


def wrap_fname_in_quotes_if_needed(fn: str) -> str:
    """If a filename has spaces, or is empty, wrap it in quotes."""
    return fn if (" " not in fn and len(fn) > 0) else f'"{fn}"'


def create_new_job_config(
    ivert_args: argparse.Namespace, verbose: bool = True
) -> tuple[str, str, list[str]]:
    """Create a new job Config file to upload to the S3 bucket.

    Saves the Config file locally and returns a path to it.

    Args:
        ivert_args (argparse.Namespace): The parsed arguments from the command line given to client.py, or a slightly-modified version.
            See client.py for listings of these arguments.
        verbose (bool, optional): Whether to print the path to the new job Config file. Defaults to True.

    Returns:
        str: The path to the new job Config file.
        str: The upload prefix for the new job Config file.
        list[str]: The list of other files to upload.
    """
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config()

    # Make a copy of the namespace that we can modify for submission.
    args = argparse.Namespace(**vars(ivert_args))
    # Make sure there's a command args in there, and that it's one of the allowed commands.
    assert hasattr(args, "command")
    args.command = args.command.strip().lower()
    assert args.command in ivert_config.ivert_commands

    username, new_job_number = create_new_job_params(ivert_config.username)

    # Genereate the full upload prefix for this new job.
    upload_prefix = str(
        os.path.join(
            ivert_config.s3_import_untrusted_prefix_base,
            args.command,
            username,
            str(new_job_number),
        )
    )
    # AWS S3 paths only use forward-slashes.
    upload_prefix = upload_prefix.replace(os.path.sep, "/")

    job_name = f"{username}_{new_job_number}"

    command = args.command

    files = None
    # Gather the list of files to include in the job Config file.
    if hasattr(args, "files"):
        if type(args.files) is str:
            files = [args.files]
        elif type(args.files) in [set, list, tuple]:
            files = args.files
        else:
            raise ValueError(f"Unexpected type of 'files' argument: {type(args.files)}")

        files_out = []
        # Check to see if any files contain a glob pattern. If so, expand it.
        for fn in files:
            if s3.S3Manager.contains_glob_flags(fn):
                matching_fnames = glob.glob(os.path.expanduser(fn))
                files_out.extend(matching_fnames)
            else:
                files_out.append(os.path.abspath(os.path.expanduser(fn)))

        del args.files

    else:
        files_out = []

    files_text = repr([os.path.basename(fn) for fn in files_out])

    # These variables are stored elsewhere in the Config file. Remove them from the namespace of "extra arguments"
    if hasattr(args, "command"):
        del args.command
    if hasattr(args, "username"):
        del args.username
    if hasattr(args, "user"):
        del args.user

    # Get the command arguments as a string. Remove the "Namespace(...)" part of the string.
    cmd_args_text = repr(vars(args))
    if len(cmd_args_text) == 0:
        cmd_args_text = "{}"

    ivert_version = version.__version__

    # Now that we've gathered all the fields needed, insert them into the Config template text.
    config_text = grab_job_config_template()
    config_text = (
        config_text.replace("[USERNAME]", username)
        .replace("[JOB_ID]", str(new_job_number))
        .replace("[JOB_NAME]", job_name)
        .replace("[JOB_UPLOAD_PREFIX]", upload_prefix)
        .replace("[JOB_COMMAND]", command)
        .replace("[IVERT_VERSION]", ivert_version)
        .replace("[LIST_OF_FILES]", files_text)
        .replace("[PARAMS_STRING]", cmd_args_text)
    )

    # Create the new job local directory if it doesn't exist.
    local_jobdir = os.path.join(ivert_config.ivert_jobs_directory_local, job_name)
    if not os.path.exists(local_jobdir):
        os.makedirs(local_jobdir)

    # Write out the new Config file.
    new_job_config_fname = os.path.join(local_jobdir, f"{job_name}.ini")
    with open(new_job_config_fname, "w") as f:
        f.write(config_text)

    if verbose:
        print(f"{new_job_config_fname} created.")

    return new_job_config_fname, upload_prefix, files_out


def grab_job_config_template() -> str:
    global ivert_config
    if not ivert_config:
        ivert_config = configfile.Config()

    job_template_file = ivert_config.ivert_job_config_template
    assert os.path.exists(job_template_file)
    return open(job_template_file, "r").read()


def upload_new_job(args: argparse.Namespace, verbose=True) -> str:
    """Upload a new job to the S3 bucket.

    Will create a local Config file and upload it to the S3 bucket along with all the files associated with the job.
    """
    new_job_config_fname, upload_prefix, list_of_other_files = create_new_job_config(
        args, verbose=verbose
    )

    job_name = os.path.splitext(os.path.basename(new_job_config_fname))[0]

    numfiles = len(list_of_other_files) + 1
    if verbose:
        print(f"Uploading {numfiles} files to the S3 bucket at {upload_prefix}/")

    # Upload the Config file to the S3 bucket.
    s3m = s3.S3Manager()
    s3m.upload(
        new_job_config_fname,
        upload_prefix + "/" + os.path.basename(new_job_config_fname),
        bucket_type="untrusted",
    )

    if verbose:
        progress_bar.ProgressBar(1, numfiles, decimals=0, suffix=f"1/{numfiles}")

    # Upload the other files to the S3 bucket.
    for i, f in enumerate(list_of_other_files):
        s3m.upload(
            f, upload_prefix + "/" + os.path.basename(f), bucket_type="untrusted"
        )

        if verbose:
            progress_bar.ProgressBar(
                2 + i, numfiles, decimals=0, suffix=f"{2 + i}/{numfiles}"
            )

    return job_name


if __name__ == "__main__":
    print(create_new_job_params())
