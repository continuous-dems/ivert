"""A helper utility for listing all photon tiles in an S3 bucket."""

import logging
import os

import click

from ivert import s3
from ivert.utils import configfile

logger = logging.getLogger(__name__)


def write_photon_tiles_to_file(outfile: str):
    """Write all photon tiles in an S3 bucket to a file."""
    ivert_config = configfile.Config()
    if ivert_config.is_aws:
        s3m = s3.S3Manager()
        ptile_prefix = ivert_config.s3_photon_tiles_directory_prefix
        # Make sure it ends in a "/"
        ptile_prefix = ptile_prefix + ("" if ptile_prefix[-1] == "/" else "/")
        fnames = s3m.listdir(ptile_prefix, bucket_type="database", recursive=False)
        # Get rid of any subdirectories listed and strip off the prefix.
        fnames = [fn.split("/")[-1] for fn in fnames if (fn[-1] != "/")]
        # Only include files that start with "photon_tile"
        fnames = sorted([fn for fn in fnames if fn.startswith("photon_tile")])

    else:
        dirname = ivert_config.icesat2_photon_tiles_directory
        # Get rid of any subdirectories listed.
        fnames = sorted(
            [
                fn
                for fn in os.listdir(dirname)
                if (
                    (not os.path.isdir(os.path.join(dirname, fn)))
                    and fn.startswith("photon_tile")
                )
            ],
        )

    with open(outfile, "w", encoding="utf-8") as f:
        f.writelines(fn + "\n" for fn in fnames)

    logger.info("Wrote %s photon tiles to %s.", len(fnames), outfile)


@click.command(help="List all photon tiles in an S3 bucket and write out to a file.")
@click.argument("outfile")
def main(outfile):
    """List all photon tiles in an S3 bucket and write out to a file.

    OUTFILE is the name of the output file.
    """
    write_photon_tiles_to_file(outfile)


if __name__ == "__main__":
    main()
