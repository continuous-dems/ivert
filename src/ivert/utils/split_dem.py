#!python3
"""Quick utility for splitting a large DEM into sub-segments to ease processing constraints."""

import argparse
import glob
import itertools
import os
import subprocess
import shlex
import sys
import typing
from osgeo import gdal

gdal.UseExceptions()


def contains_glob_flags(fname: str) -> bool:
    """Return True if a string contains any glob-style wildcard flags."""

    return ("*" in fname) or ("?" in fname) or ("[" in fname and "]" in fname)


def split(dem_name: typing.Union[str, list[str]],
          factor: int = 2,
          output_dir: typing.Union[str, None] = None,
          verbose: bool = True) -> list[str]:
    """Split a DEM into sub-segments, each side split by a factor. 2 will create 4 sub-segments.

    Args:
        dem_name (str, list): The name of the DEM, with path, or a list of DEM names.
        factor (int): The factor by which to split the DEM.
        output_dir (str): The directory to which the sub-segments will be written. Defaults to the same directory as the DEM.
        verbose (bool): Whether to print messages.

    Returns:
        list[str]: The names of the new DEM files.
        """
    if isinstance(dem_name, str):
        dem_name = [dem_name,]

    if output_dir is None:
        output_dir = os.path.dirname(dem_name[0])

    outfiles = []
    infiles = []

    # Expand the number of files if there are glob flags.
    for dname in dem_name:
        if contains_glob_flags(dname):
            infiles.extend(glob.glob(dname))
        else:
            infiles.append(dname)

    # some bug here?
    for fname in infiles:
        Y, X = gdal.Open(fname, gdal.GA_ReadOnly).ReadAsArray().shape
        # How to cover all the DEM when it can't be split evenly?
        X_steps = evenly_split(X, factor)
        Y_steps = evenly_split(Y, factor)

        for xi, xb in zip(range(factor), X_steps):
            for yj, yb in zip(range(factor), Y_steps):
                assert len(xb) == 2
                assert len(yb) == 2
                fn_out = os.path.join(output_dir, f"{os.path.splitext(os.path.basename(fname))[0]}_{yj}.{xi}.tif")

                if os.path.exists(fn_out):
                    print(fn_out, "already exists.")
                    continue

                gdal_cmd = f"""gdal_translate -of GTiff
                              -srcwin {xb[0]} {yb[0]} {xb[1] - xb[0] + 1} {yb[1] - yb[0] + 1}
                              -co COMPRESS=DEFLATE -co PREDICTOR=2 -co TILED=YES
                              {repr(fname)} {repr(fn_out)}"""

                gdal_args = shlex.split(gdal_cmd.replace("\n", " "))
                print(" ".join(gdal_args))

                subprocess.run(gdal_args)

                if os.path.exists(fn_out):
                    if verbose:
                        print(fn_out, "written.")
                        outfiles.append(fn_out)
                else:
                    if verbose:
                        print(fn_out, "failed.")

    return outfiles


def evenly_split(N: int, factor: int) -> list:
    """Split N evenly into factor pieces by index.

    If it doesn't split evenly, add an extra to the last (remainder) pieces to make it as even as possible.

    Returns the starting and ending index of each sub-segment.
    """
    batches_all = list(itertools.batched(range(N), N // factor))
    if len(batches_all) == factor:
        batches = [(b[0], b[-1]) for b in batches_all]
    else:
        assert len(batches_all) == (factor + 1)
        batches = [(b[0], b[-1]) for b in batches_all[:-1]]
        extras = batches_all[-1]
        assert len(extras) < len(batches)

        M = len(extras)
        for i in range(M):
            j = -(i + 1)
            batches[j] = (batches[j][0] + (M + j), batches[j][1] + (M + j + 1))

        assert len(batches) == factor

    return batches

def define_and_parse_args():
    parser = argparse.ArgumentParser(description="Split a DEM into sub-segments, each side split by a factor.")
    parser.add_argument("dem_name", nargs="+",
                        help="The name of the DEM file. May use bash-style glob flags (*.tif) to select multiple files.")
    parser.add_argument("-f", "--factor", type=int, default=2,
                        help="The factor by which to split each side of the DEM. This will create f^2 files.")
    parser.add_argument("-o", "--output_dir", type=str, default=None,
                        help="The directory to which the sub-segments will be written. Default: will use the same directory as the input DEM.")

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()
    split(args.dem_name, args.factor, args.output_dir)
