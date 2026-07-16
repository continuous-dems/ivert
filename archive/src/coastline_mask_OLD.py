# -*- coding: utf-8 -*-

"""coastline_mask_OLD.py -- Wrapper code for using the CUDEM "coastline" module for
creating resampled coastline masks for DEMs.

TODO: Look into covering interior water bodies with this, which we don't currently do
except over the US. There are some LandSat & MODIS-sourced datasets we could use for this, I think.
"""

try:
    # We don't actually use the cudem modules here, but we make command-line
    # calls to "waffles", so check here to make sure cudem is installed on this
    # machine. If we can import the waffles module, we can use it from the command-line.
    pass
except:
    raise ModuleNotFoundError(
        "Module 'cudem/waffles.py' required. Update paths, or refer to https://github.com/ciresdem/cudem for installation instructions."
    )

import argparse
import os
from osgeo import gdal
import rich.console
import subprocess

# import pyproj
import shutil
import sys
import time
import typing

# Use Config file to get the encrypted credentials.
import utils.pyproj_funcs
import utils.configfile as configfile
import utils.query_yes_no as yes_no

ivert_config = configfile.Config()
gdal.UseExceptions()


def is_this_run_in_ipython():
    """Tell whether we're running in an IPython console or not. Useful for rich.print()."""
    try:
        # When using IPython, the __IPYTHON__ variable is defined.
        __IPYTHON__
        return True

    except NameError:
        # Outside of IPython, referencing the variable fails with a NameError. Just return False.
        return False


def create_coastline_mask(
    input_dem,
    return_bounds_step_epsg: bool = False,
    mask_out_lakes: bool = True,
    include_gmrt: bool = False,  # include_gmrt will include more minor outlying islands, many of which copernicus leaves out but GMRT includes
    mask_osm_buildings: bool = True,
    mask_bing_buildings: bool = True,
    mask_wsf_urban: bool = False,
    mask_out_nhd: bool = True,
    output_file: bool = None,
    run_in_tempdir: bool = False,
    horizontal_datum_only: bool = True,
    verbose: bool = True,
):
    """
    From a given DEM (.tif or otherwise), generate a coastline mask at the same grid and resolution.

    Uses the cudem waffles utility, which is handy for this.

    If output_file is None, put it in the same directory as the input_dem with the same base name.

    If return_ds_bounds_step_espg,
        Then return, in addition to the final mask output path.
        - The GDAL dataset of the DEM
        - The DEM bounding box (xmin, xmax, ymin, ymax)
        - The DEM pixel size in tuple: (xstep,ystep)
        - The DEM EPSG code

    If "round_up_half_pixel" is True, we expand the bounding-box size when performing
    the waffles command by an extra half-pixel in each direction. This eliminates occasional
    rounding errors where the pixel size causes the grid to be shortened by one pixel
    along one of the axes. It does not affect the bounding box returned to the calling function.

    if "horizontal_datum_only", if the input DEM is a combined horizontal+vertical datum, create in the horizontal datum only.

    Return the .tif name of the mask file generated."""

    input_ds = gdal.Open(input_dem, gdal.GA_ReadOnly)
    if not input_ds:
        raise FileNotFoundError("Input file '{input_dem}' not found.")

    bbox, step_xy = utils.pyproj_funcs.get_bounding_box_and_step(
        input_ds, bbox_interleaved=False
    )
    # print(bbox, step_xy)

    epsg = utils.pyproj_funcs.get_dataset_epsg(
        input_ds, horizontal_only=horizontal_datum_only
    )
    # print(epsg)

    if output_file:
        output_filepath_base = os.path.splitext(output_file)[0]
    else:
        output_filepath_base = os.path.splitext(input_dem)[0] + "_coastline_mask"

    # output_dir, output_filebase = os.path.split(output_filepath_base)

    # Run a rich-text console for the output.
    console = rich.console.Console(
        force_jupyter=(True if is_this_run_in_ipython() else None)
    )
    # Sometimes waffles can give some rounding error effects if the boundaries aren't exactly right.
    # If we round up half a pixel on each file extent size, it can ensure we include everything.

    waffle_args = [
        "waffles",
        "-M",
        "coastline:polygonize=False"
        + (":want_gmrt=True" if include_gmrt else "")
        + (":want_lakes=True" if mask_out_lakes else "")
        + (":want_nhd_plus=" + str(mask_out_nhd))
        + (":want_osm_buildings=True" if mask_osm_buildings else "")
        + (":want_bing_buildings=True" if mask_bing_buildings else "")
        + (":want_wsf_urban=True" if mask_wsf_urban else ""),
        "-R",
        "{0}/{1}/{2}/{3}".format(*bbox),
        "-O",
        os.path.abspath(output_filepath_base),
        "-P",
        "epsg:{0:d}".format(epsg),
        "-E",
        str("{0:.16f}/{1:.16f}".format(step_xy[0], step_xy[1])),
        "-D",
        ivert_config.cudem_cache_directory,
        "--keep-cache",
        "--nodata",
        str(ivert_config.dem_default_ndv),
        "-co",
        "COMPRESS=DEFLATE",
        "-co",
        "PREDICTOR=2",
        "-co",
        "TILED=YES",
    ]

    if verbose:
        console.print(
            "Running: [bold green]"
            + waffle_args[0]
            + "[/bold green] "
            + " ".join(waffle_args[1:])
        )
        # console.print("...in directory {}".format(output_dir))

    if verbose:
        kwargs = {}
    else:
        # Capture output and direct to /dev/null if we're running non-verbose.
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    # Put the data files generated in this processin the same directory as the output file.
    # This will be the home working directory of the process.
    tempdir = None
    if run_in_tempdir:
        tempdir = os.path.join(
            ivert_config._abspath(ivert_config.cudem_cache_directory),
            "temp" + str(os.getpid()),
        )
        if not os.path.exists(tempdir):
            os.mkdir(tempdir)
        kwargs["cwd"] = tempdir

    ret_proc = subprocess.run(
        waffle_args, check=True, stdout=sys.stdout, stderr=sys.stderr, **kwargs
    )

    if os.path.splitext(output_filepath_base)[1].lower() != ".tif":
        final_output_path = os.path.join(output_filepath_base + ".tif")
    else:
        final_output_path = output_filepath_base

    if not os.path.exists(final_output_path) and verbose:
        print(os.path.basename(final_output_path), "NOT written.")

        # TODO: Check return value, if the process was killed by a memory error, divide and conquer. Otherwise, just clear the cache and try again up to N number of times.

    if run_in_tempdir:
        shutil.rmtree(tempdir, ignore_errors=True)

    if return_bounds_step_epsg:
        return final_output_path, bbox, step_xy, epsg
    else:
        return final_output_path


# 2: ICESat-2 data acquisition:
# a) Get a file list from the NSIDC/icepyx API
# b) Check existing files to see what we already have.
# c) Download additional files of ATL03/ATL06/ATL08 data, where applicable.


def create_coastal_mask_filename(
    dem_name: str, target_dir: typing.Union[str, None] = None
):
    """If given a DEM name, create a filename for the coastal mask."""
    if type(target_dir) == str and (len(target_dir.strip()) == 0):
        target_dir = None

    fdir, fname = os.path.split(os.path.abspath(dem_name))
    base, ext = os.path.splitext(fname)
    coastline_mask_fname = os.path.join(
        fdir if (target_dir is None) else target_dir, base + "_coastline_mask" + ext
    )
    return coastline_mask_fname


def get_coastline_mask_and_other_dem_data(
    dem_name,
    mask_out_lakes: bool = True,
    mask_osm_buildings: bool = True,
    mask_bing_buildings: bool = True,
    mask_wsf_urban: bool = False,
    include_gmrt=False,
    target_fname_or_dir=None,
    run_in_tempdir=False,
    return_coastline_array_only=False,
    band_num: int = 1,
    verbose=True,
):
    """Get data from the DEM and a generated/opened coastline mask.

    Return, in this order:
        1. DEM GDAL dataset,
        2. DEM array (read from the dataset already)
        3. DEM bounding box (xmin, xmax, ymin, ymax)
        4. DEM EPSG code (horizontal datum only)
        5. DEM resolution (xstep, ystep)
        6. coastline mask array in the same grid as the dem.
            (here derived from Copernicus data using the CUDEM "waffles" command))
    """
    if (
        (target_fname_or_dir is None)
        or (len(target_fname_or_dir.strip()) == 0)
        or os.path.isdir(target_fname_or_dir)
    ):
        coastline_mask_file = create_coastal_mask_filename(
            dem_name, target_fname_or_dir
        )
    else:
        coastline_mask_file = target_fname_or_dir

    if not return_coastline_array_only:
        dem_ds = gdal.Open(dem_name, gdal.GA_ReadOnly)
        dem_array = dem_ds.GetRasterBand(band_num).ReadAsArray()

    coastline_ds = None

    # Get a coastline mask (here from Copernicus). If the file exists, use it.
    # If not, generate it.
    # Occasionally this operation fails because of remote errors from the WSL server. If that's the case, try several times.
    attempt = 1
    max_attempts = 10
    while attempt <= max_attempts:
        if not os.path.exists(coastline_mask_file):
            if verbose:
                print("Creating", coastline_mask_file)
            coastline_mask_file_out = create_coastline_mask(
                dem_name,
                mask_out_lakes=mask_out_lakes,
                mask_out_nhd=mask_out_lakes,
                mask_osm_buildings=mask_osm_buildings,
                mask_bing_buildings=mask_bing_buildings,
                mask_wsf_urban=mask_wsf_urban,
                include_gmrt=include_gmrt,
                return_bounds_step_epsg=False,
                output_file=coastline_mask_file,
                run_in_tempdir=run_in_tempdir,
                verbose=verbose,
            )

            try:
                assert coastline_mask_file == coastline_mask_file_out
            except AssertionError as e:
                print(
                    "coastline_mask_file:",
                    coastline_mask_file,
                    "\ncoastline_mask_file_out:",
                    coastline_mask_file_out,
                )
                raise e

        if verbose:
            print("Reading", coastline_mask_file + "...", end="")
        coastline_ds = gdal.Open(coastline_mask_file, gdal.GA_ReadOnly)
        if coastline_ds is None:
            print("UNSUCCESSFUL. Sleeping 1 minute to try again.")
            attempt += 1
            time.sleep(60)
            continue
        if verbose:
            print("Done.")

        if not return_coastline_array_only:
            dem_bbox, dem_step_xy = utils.pyproj_funcs.get_bounding_box_and_step(dem_ds)
            dem_epsg = utils.pyproj_funcs.get_dataset_epsg(dem_ds, horizontal_only=True)

        coastline_mask_array = coastline_ds.GetRasterBand(1).ReadAsArray()
        del coastline_ds

        if return_coastline_array_only:
            return coastline_mask_array
        else:
            return (
                dem_ds,
                dem_array,
                dem_bbox,
                dem_epsg,
                dem_step_xy,
                coastline_mask_file,
                coastline_mask_array,
            )

    raise FileNotFoundError(
        "Could not generate", os.path.basename(coastline_mask_file_out)
    )


def read_and_parse_args() -> argparse.Namespace:
    """Read and parse the command line arguments."""
    parser = argparse.ArgumentParser(
        description="A script for creating coastline water masks from a DEM file. "
        "Return array is (0,1) for (water,land)."
    )
    parser.add_argument("dem_filename", type=str, help="Input DEM.")
    parser.add_argument(
        "dest",
        nargs="?",
        default="",
        help="Destination file name, or file directory. If name is omitted: adds '_coastline_mask' "
        "to the input file name in the same directory.",
    )
    parser.add_argument(
        "-mob",
        "--mask_osm_buildings",
        dest="mask_osm_buildings",
        type=yes_no.interpret_yes_no,
        default=True,
        help="Whether to mask out OSM-derived building footprints in the coastline mask. "
        "Must be followed by 'True', 'False', 'Yes', 'No', or any abbreviation thereof "
        "(case-insensitive). (Default: True)",
    )
    parser.add_argument(
        "-mbb",
        "--mask_bing_buildings",
        dest="mask_bing_buildings",
        type=yes_no.interpret_yes_no,
        default=True,
        help="Whether to mask out Bing-derived building footprints in the coastline mask. "
        "Must be followed by 'True', 'False', 'Yes', 'No', or any abbreviation thereof "
        "(case-insensitive). (Default: True)",
    )
    parser.add_argument(
        "-mwsf",
        "--mask_wsf_urban",
        dest="mask_wsf_urban",
        type=yes_no.interpret_yes_no,
        default=False,
        help="Whether to mask out World-Settlement-Footprint heavy urban areas in the "
        "coastline mask. Typically used instead of building footprints for coarse DEMs "
        "with grid cells larger than typical buildings (~20-ish m). Must be followed by "
        "'True', 'False', 'Yes', 'No', or any abbreviation thereof (case-insensitive). "
        "(Default: False)",
    )
    parser.add_argument(
        "--use_gmrt",
        default=False,
        action="store_true",
        help="Include land areas covered by the GMRT land-cover dataset. Including GMRT is useful for "
        "including many small outlying islands that Copernicus may exclude.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", default=False, help="Run quietly."
    )

    args = parser.parse_args()
    return args


if __name__ == "__main__":
    args = read_and_parse_args()

    create_coastline_mask(
        args.dem_filename,
        return_bounds_step_epsg=False,
        mask_osm_buildings=args.mask_osm_buildings,
        mask_bing_buildings=args.mask_bing_buildings,
        mask_wsf_urban=args.mask_wsf_urban,
        include_gmrt=args.use_gmrt,
        output_file=None if (args.dest.strip() == "") else args.dest,
        verbose=not args.quiet,
    )
