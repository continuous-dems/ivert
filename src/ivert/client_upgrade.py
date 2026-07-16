"""Upgrade the IVERT client."""

import shlex
import subprocess

import ivert.utils.configfile as configfile


def upgrade():
    """Upgrade the IVERT client."""
    ivert_config = configfile.Config()

    # Run the upgrade, using the pip command specified in the Config file.
    args = shlex.split(ivert_config.ivert_pip_upgrade_command)
    subprocess.run(args)

    return


if __name__ == "__main__":
    upgrade()
