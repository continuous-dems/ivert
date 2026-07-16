# -*- coding: utf-8 -*-

"""convert_vdatum.py -- Code for converting vertical datums of GeoTiff raster grids.
Uses gdalwarp as the underlying tool.

Author: Mike MacFerrin, University of Colorado
Created: August 18, 2021
"""

# gdalwarp -s_srs EPSG:4326 -t_srs EPSG:4326 -tr 0.0002777777777777778 0.0002777777777777778 -te -79.0000000000000000 26.0000000000000000 -78.0000000000000000 27.0000000000000000 -r cubic -of GTiff -overwrite /home/mmacferrin/Research/DATA/DEMs/geoids/egm96-15.tif /home/mmacferrin/Research/DATA/DEMs/AW3D30/data/tiles/N025W080_N030W075/ALPSMLC30_N026W079_EGM96.tif
# Creating output file that is 3600P x 3600L.
# Processing /home/mmacferrin/Research/DATA/DEMs/geoids/egm96-15.tif [1/1] : 0Using internal nodata values (e.g. -32767) for image /home/mmacferrin/Research/DATA/DEMs/geoids/egm96-15.tif.
# Copying nodata values from source /home/mmacferrin/Research/DATA/DEMs/geoids/egm96-15.tif to destination /home/mmacferrin/Research/DATA/DEMs/AW3D30/data/tiles/N025W080_N030W075/ALPSMLC30_N026W079_EGM96.tif.

try:
    import cudem
    import utils.loggerproc
except ImportError as e:
    print(
        "Error: cudem installation required to run the vertcal_datum_convert.py script from convert_vdatum.py."
    )
    raise e
# Just an empty reference to get the above import statement to stop giving a
# constant "warning" for importing an unused library. This doesn't "do" anything.
cudem

import argparse
import os
import shutil
import signal
import subprocess
import re
import shlex
import sys

import utils.traverse_directory
import utils.configfile

my_config = utils.configfile.Config()

# Vertical datum dictionary -- for converting vertical datums to their corresponding EPSG numbers
vdd_lookup_dict = {
    "wgs84": 7912,  # WGS84 has numerous iterations. In this case, go with the ITRF 2014 datum
    "itrf2014": 7912,
    "navd88": 5703,  # Northa American Vertical Datum of 1988. Used in CONUS
    "guvd04": 6644,  # Guam Vertical Datum 2004
    "nmvd03": 6644,  # Northern Marianas Vertical Datum 2003, same as GUVD04
    "asvd02": 6643,  # American Samoa 2002
    "prvd02": 6641,  # Puerto Rico 2002
    "vivd09": 6641,  # Virgin Islands VD 2009 (same grid used as PRVD02)
    "egm2008": 3855,  # Earth Gravitational Model 2008
    "egm96": 5773,  # Earth Gravitational Model 1996
    "egm84": 5798,
    # Earth Gravitational Model 1984 (not currently implemented.) NOTE: Can use/resample the EGM84 global grid and do it myself if we want.
    "msl": 5174,  # Mean Sea Level
    "wgs84(g1150)": 7660,
    "cgvd2013(cgg2013)": 6647,
    "cgvd2013": 6647,
    "cgvd2013(cgg2013a)": 9245,
}

vdd_descriptions_dict = {
    "wgs84": "World Geodetic System 1984 vertical datum (using ITRF 2014 Iteration, identical to 'itrf2014'). EPSG: 7912",
    "itrf2014": "International Terrestrial Reference Fram 2014, vertical heights. EPSG: 7912",
    "navd88": "Nortah American Vertical Datum of 1988. EPSG: 5703",
    "guvd04": "Guam Vertical Datum of 2004. EPSG: 6644",
    "nmvd03": "Northern Marianas Vertical Datum of 2003. Same as GUVD04 (EPSG: 6644)",
    "asvd02": "American Samoa Vertical Datum of 2002. EPSG: 6643",
    "prvd02": "Puerto Rico 2002. EPSG: 6641",
    "vivd09": "Virgin Islands 2009. EPSG: 6641 (uses same grid as PRVD02)",
    "egm2008": "Earth Gravitational Model of 2008, vertical heights. EPSG: 3855",
    "egm96": "Earth Gravitational Model of 1996, vertical heights. EPSG: 5733",
    "egm84": "Earth Gravitational Model of 1984, vertical heights. EPSG: 5798 (not currently implemented)",
    "msl": "Mean Sea Level. EPSG: 5174",
    "wgs84(g1150)": "WGS 84 vertical datum, using G1150 iteration. EPSG: 7660",
    "cgvd2013(cgg2013)": "Canadian vertical datum.",
    "cgvd2013": "Canadian vertical datum",
    "cgvd2013(cgg2013a)": "Canadian vetical datum",
}

# A list of all supported vdatum names and values in this module. Useful for other
# modules to check.
SUPPORTED_VDATUMS = list(vdd_lookup_dict.keys()) + list(vdd_lookup_dict.values())
# Not currently supporting EGM84. Remove from this list for now.
SUPPORTED_VDATUMS.remove("egm84")
SUPPORTED_VDATUMS.remove(5798)


def list_all_datums():
    for key in sorted(list(vdd_lookup_dict.keys())):
        print("{0:<8s}".format(key), ":", vdd_descriptions_dict[key])


def vertical_datum_lookup(vd_name):
    """Given a string name or number, return the EPSG integer corresponding with that vertical datum."""
    try:
        return int(vd_name)
    except ValueError:
        try:
            return vdd_lookup_dict[vd_name.lower().strip()]
        except KeyError:
            return vd_name.lower().strip()


def convert_vdatum(
    input_dem,
    output_dem,
    input_vertical_datum="itrf2014",
    output_vertical_datum="itrf2014",
    cwd=None,
    verbose=True,
):
    """Input a DEM of a known vetical datum, and convert the elevations to another vertical datum.

    input_vertical_datum and output_vertical datum can be abbreviated names (capital or lower-case), or EPSG numbers.

    TODO: Test whether this automatically respects NDVs (not sure here).
    If not, institute empty-value replacement after conversion.
    """

    if type(input_vertical_datum) != int:
        input_vertical_datum = vertical_datum_lookup(input_vertical_datum)

    if type(output_vertical_datum) != int:
        output_vertical_datum = vertical_datum_lookup(output_vertical_datum)

    # If both the input and output datums are the same, just copy the file unchanged.
    if input_vertical_datum == output_vertical_datum:
        shutil.copyfile(input_dem, output_dem)
        return 0

    # Run the vdatums command under the hood. The "-wm" flag is forwarded on to gdalwarp in order to limit the memory
    # usage of the process to 50% of available memory and help ensure it doesn't crash.
    # Requires cudem v 2.3.16 or higher (vdatums v0.2 or higher).
    command_template = "vdatums {3:s} {4:s} -i {0} -o {1} -D {2:s} --keep-cache -wm 50%"
    command = command_template.format(
        input_vertical_datum,
        output_vertical_datum,
        my_config.cudem_cache_directory,
        input_dem.replace(" ", r"\ "),
        output_dem.replace(" ", r"\ "),
    )

    if verbose:
        print("Running:", command)
    retproc = subprocess.run(
        shlex.split(command),
        capture_output=not verbose,
        stdout=sys.stdout,
        stderr=sys.stderr,
        cwd=cwd,
    )

    if verbose and retproc.returncode != 0:
        if abs(retproc.returncode) == abs(signal.SIGKILL):
            pass
            # TODO: Divide and conquer. See validate_dem.py lines 616-ish ("elif abs(exitcode) == abs(signal.SIGKILL):")
            #   and do similar here.

        print(
            "ERROR: Process\n'{0}'\n... returned status code {1}.".format(
                command, retproc.returncode
            )
        )

    return retproc.returncode


def define_args():
    """convert_vdatum.py -i [input_vdatum] -o [output_vdatum] -output_suffix [output_suffix]
    -input_file_filter [input_file_filter] -output_folder [output_folder] -output_filename [output_filename]
    --list_vdatums
    input_DEM_or_directory
    """
    parser = argparse.ArgumentParser(
        description="A quick tool for converting vertical datums of a DEM or directory "
        "of DEMs to other vertical datums."
    )
    parser.add_argument(
        "-input_vdatum",
        "-i",
        type=str,
        default="itrf2014",
        help="Input vertical datum.",
    )
    parser.add_argument(
        "-output_vdatum",
        "-o",
        type=str,
        default="itrf2014",
        help="Output vertical datum.",
    )
    parser.add_argument(
        "-input_file_filter",
        "-filter",
        type=str,
        default=r".tif\Z",
        help=r"Regex string to search for in input file names. Ignore all other files. "
        r"Default '.tif\Z', indicating .tif at the end of the string.",
    )
    parser.add_argument(
        "-output_folder",
        "-dir",
        default=None,
        help="Directory in which to put the output files. Ignored if -output_filename is given. "
        "Default: Use same directory as input file directory.",
    )
    parser.add_argument(
        "-output_subdir",
        "-sdir",
        default=None,
        help="Put the output file in a relative sub-directory of the input file. "
        "This will override any '-output_folder' argument.",
    )
    parser.add_argument(
        "-output_filename",
        "-of",
        default=None,
        help="Output filename. Only used if the input_DEM_or_directory is a single file "
        "(and not a directory). If a directory is listed, this option is ignored and "
        "the suffix specified in '-output_suffix' is appended onto each of the input file names.",
    )
    parser.add_argument(
        "-output_suffix",
        "-suffix",
        type=str,
        default="_out",
        help="Suffix to append to input file(s) to create output file name. Ignored if "
        "-output_filename is set. If set, files that already end in this suffix will be "
        "ignored as input files.",
    )
    parser.add_argument(
        "--list_vdatums",
        "--list",
        action="store_true",
        default=False,
        help="Print a list of all supported vertical datums in the tool. Ignore all other arguments. "
        "If this argument is omitted, an argument must be provided for [input_DEM_or_directory].",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Execute quietly (unless an error occurs). Ignored if --help or --list_vdatums "
        "is selected.",
    )
    parser.add_argument(
        "--recurse",
        "-r",
        default=False,
        action="store_true",
        help="Recurse through the directory to find matching files (default: only look in the "
        "local directory).",
    )
    parser.add_argument(
        "--overwrite",
        "-ov",
        default=False,
        action="store_true",
        help="Overwrite output files. Default: Skip generating output file if it already exists.",
    )
    parser.add_argument(
        "--nprocs",
        default=1,
        help="Number of parallel processes to use. Default 1 (serial execution).",
    )
    parser.add_argument(
        "input_DEM_or_directory",
        nargs="?",
        default=None,
        help="Input DEM file or a directory of DEM files. If a single existing file is given, "
        "-input_file_filter is ignored and the file is read, and the converted file is "
        "written either to -output_filename location (if given), or is put in the "
        "-output_directory with the -output_suffix applied to the filename. If a directory "
        "is given, -input_file_filter is applied to identify input files, and the outputs are "
        "written to the -output_filter (if given).",
    )

    return parser


if __name__ == "__main__":
    parser = define_args()

    args = parser.parse_args()

    # print(args.input_DEM_or_directory) -- doesn't return a list... either gives None or the argument
    if args.list_vdatums:
        print()
        list_all_datums()
        print(
            "\nVertical datums may be input either using abbreviations (left) or EPSG numbers."
        )
        sys.exit(0)
    elif args.input_DEM_or_directory is None:
        print(
            "If --list_vdatums not used, then at least one argument must be provided for [input_DEM_or_directory].\n"
        )
        parser.print_help()
        sys.exit(0)

    assert (
        args.input_DEM_or_directory is not None
    )  # Shouldn't logically get here if this argument is still empty. Sanity Check.

    # Check to see if the file/directory exists. If not, toss an error.
    if not os.path.exists(args.input_DEM_or_directory):
        raise FileNotFoundError(
            "ERROR: '{0}' not found.".format(args.input_DEM_or_directory)
        )

    if os.path.isdir(args.input_DEM_or_directory):
        # A directory is given here.
        # 1. Get a list of files in the directory.
        if args.recurse:
            fnames = utils.traverse_directory.list_files(
                args.input_DEM_or_directory, include_base_directory=True
            )
        else:
            fnames = [
                os.path.join(args.input_DEM_or_directory, fn)
                for fn in sorted(os.listdir(args.input_DEM_or_directory))
            ]

        # 2. Filter out files according to -input_file_filter
        if args.input_file_filter:
            # Use only names that pass the file filter screening.
            fnames = [
                fn
                for fn in fnames
                if (re.search(args.input_file_filter, os.path.basename(fn)) != None)
            ]
        if args.output_suffix:
            # Omit name that have the exact same suffix as the -output_suffix flag
            fnames = [
                fn
                for fn in fnames
                if (
                    os.path.splitext(fn)[0][-len(args.output_suffix) :]
                    != args.output_suffix
                )
            ]

        # 4. Loop thorugh, call vdatum_convert() on each of them.
        for i, fn in enumerate(fnames):
            base, ext = os.path.splitext(fn)
            basedir, basename = os.path.split(base)

            # 3. Create a list of output files using the -output_folder and -output_suffix
            if args.output_subdir:
                output_folder = os.path.join(basedir, args.output_subdir)
            elif args.output_folder:
                output_folder = args.output_folder
            else:
                output_folder = args.input_DEM_or_directory

            output_filename = os.path.join(
                output_folder,
                basename + (args.output_suffix if args.output_suffix else "") + ext,
            )

            # TODO: Implement parallel processing here.

            if os.path.exists(output_filename):
                if args.overwrite:
                    os.remove(output_filename)
                else:
                    print(output_filename, "already exists. Moving on.")
                    continue

            convert_vdatum(
                fn,
                output_filename,
                input_vertical_datum=args.input_vdatum,
                output_vertical_datum=args.output_vdatum,
                verbose=not args.quiet,
            )

    else:
        # A file was input.
        if args.output_filename is not None:
            # If an output filename was given, use that.
            output_filename = args.output_filename
        elif args.output_subdir is not None:
            # If we're given a relative sub-directory, put it there.
            base, ext = os.path.splitext(args.input_DEM_or_directory)
            basedir, basename = os.path.split(base)
            output_filename = os.path.join(
                basedir,
                args.output_subdir,
                basename + (args.output_suffix if args.output_suffix else "") + ext,
            )
        elif args.output_folder is not None:
            # If an output directory was provided, put it thre, along with the suffix at the end of the filename.
            base, ext = os.path.splitext(os.path.split(args.input_DEM_or_directory)[1])
            output_filename = os.path.join(
                args.output_folder,
                base + (args.output_suffix if args.output_suffix else "") + ext,
            )
        else:
            # Otherwise, if no output directry was given, just use the input directory, adding whatever suffix was provided.
            base, ext = os.path.splitext(args.input_DEM_or_directory)
            output_filename = (
                base + (args.output_suffix if args.output_suffix else "") + ext
            )

        if os.path.exists(output_filename):
            if args.overwrite:
                os.remove(output_filename)
            else:
                print(
                    output_filename,
                    "already exists. Use the --overwrite (-o) functino to overwrite this file.",
                )
                import sys

                sys.exit(0)

        # Convert the file.
        convert_vdatum(
            args.input_DEM_or_directory,
            output_filename,
            input_vertical_datum=args.input_vdatum,
            output_vertical_datum=args.output_vdatum,
            verbose=not args.quiet,
        )
