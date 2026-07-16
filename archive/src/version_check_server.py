import os
import sys
from packaging.version import Version

if vars(sys.modules[__name__])["__package__"] == "ivert_utils":
    # When this is built a setup.py package, it names the modules 'ivert' and 'ivert_utils'. This reflects that.
    import ivert_utils.is_aws as is_aws
else:
    try:
        # If running as a script, import this way.
        import is_aws
    except ModuleNotFoundError:
        # If this script is imported from another module in the src/ directory, import this way.
        import utils.is_aws as is_aws


def minimum_client_version():
    # This is the minimum version of the client that the server will accept.
    # This is stored in ivert/VERSION_CLIENT_MIN
    MCVFILE1 = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "VERSION_CLIENT_MIN")
    )
    MCVFILE2 = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "ivert_data",
            "VERSION_CLIENT_MIN",
        )
    )
    if vars(sys.modules[__name__])["__package__"] == "ivert_utils" and os.path.exists(
        MCVFILE2
    ):
        file_to_use = MCVFILE2
    elif os.path.exists(MCVFILE1):
        file_to_use = MCVFILE1
    else:
        raise FileNotFoundError("Could not find 'VERSION_CLIENT_MIN' file.")

    with open(file_to_use, "r") as f:
        # Strip off # comments and blank lines, then return the first line
        return [
            line.strip()
            for line in f.readlines()
            if line.strip()[0] != "#" and len(line.strip()) > 0
        ][0]


def is_compatible(client_version: str) -> bool:
    """Returns True if the given client version is compatible with the server."""
    if not is_aws.is_aws():
        raise NotImplementedError(
            "is_compatible is supported only on the AWS server. Use is_this_client_compatible instead."
        )
    return Version(client_version) >= Version(minimum_client_version())
