"""A helper utility for listing all photon tiles in an S3 bucket."""

import os
import re
import sys

import argparse

import ivert.utils.configfile as configfile
import ivert.s3 as s3


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
        fnames = sorted([fn for fn in os.listdir(dirname)
                         if ((not os.path.isdir(os.path.join(dirname, fn))) and fn.startswith("photon_tile"))])

    with open(outfile, "w") as f:
        for fn in fnames:
            f.write(fn + "\n")

    print(f"Wrote {len(fnames)} photon tiles to {outfile}.")


def define_and_parse_args():
    """Define and parse command line arguments."""
    parser = argparse.ArgumentParser(description="List all photon tiles in an S3 bucket and write out to a file.")
    parser.add_argument("outfile", help="The name of the output file.")

    return parser.parse_args()

if __name__ == "__main__":
    args = define_and_parse_args()
    write_photon_tiles_to_file(args.outfile)
