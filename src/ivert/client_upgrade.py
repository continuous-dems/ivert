"""Upgrade the IVERT client."""

import shlex
import subprocess

from ivert.utils import configfile


def upgrade():
    """Upgrade the IVERT client."""
    ivert_config = configfile.Config()

    # Run the upgrade, using the pip command specified in the Config file.
    args = shlex.split(ivert_config.ivert_pip_upgrade_command)
    subprocess.run(args)


if __name__ == "__main__":
    upgrade()
