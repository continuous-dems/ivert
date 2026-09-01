"""Validate a DEM against ICESat-2 photon elevations.

Samples classified ICESat-2 photons over a DEM, aggregates them per DEM cell,
and reports per-cell and summary error statistics. Cell-level validation runs
across child processes over shared-memory arrays; large DEMs are subdivided and
their results merged back together.

Created on Tue Jun 22 16:06:21 2021

@author: mmacferrin
"""

import ast
import contextlib
import multiprocessing as mp
import os
import re
import signal
import sys
import time
from multiprocessing import shared_memory

import click
import geopandas
import numexpr
import numpy
import pandas
import pyproj
import rasterio
import shapely
import shapely.geometry
import tqdm

import ivert.icesat2_database_v2
import ivert.plot_validation_results
import ivert.transform_points
import ivert.utils.configfile
import ivert.utils.loggerproc
import ivert.utils.split_dem
from ivert.utils import dem_geom, parallel_funcs

ivert_config = ivert.utils.configfile.Config()
EMPTY_VAL = ivert_config.dem_default_ndv
TRANSFORMEZ_CACHE_DIR = ivert_config.cache_directory

# Grid cells with at least this many photons have their outlier photons trimmed to
# the interdecile (10th-90th percentile) range before their elevation statistics are
# computed. Below this count there are too few photons to distinguish an outlier from
# the signal, so every photon in the cell is used instead.
INTERDECILE_MIN_PHOTONS = 5


def read_dataframe_file(df_filename: str) -> pandas.DataFrame:
    """Read a dataframe file, either from a picklefile, HDF, CSV, or feather.

    (Can handle other formats by adding more "elif ..." statements in the function.)
    """
    assert os.path.exists(df_filename)
    ext = os.path.splitext(df_filename)[1]
    ext = ext.lower()
    if ext == ".pickle":
        dataframe = pandas.read_pickle(df_filename)
    elif ext in (".h5", ".hdf"):
        dataframe = pandas.read_hdf(df_filename, mode="r")
    elif ext in (".csv", ".txt"):
        dataframe = pandas.read_csv(df_filename)
    elif ext == ".feather":
        dataframe = pandas.read_feather(df_filename)
    else:
        raise NotImplementedError(
            f"ERROR: Unknown dataframe file extension '{ext}'. (Currently supporting .pickle, .h5, .hdf, .csv, .txt, or .feather)",
        )

    return dataframe


def validate_dem_child_process(
    height_array_name,
    height_dtype,
    i_array_name,
    i_dtype,
    j_array_name,
    j_dtype,
    code_array_name,
    code_dtype,
    array_shape,
    connection,
    photon_limit=None,
    min_photons=3,
    measure_coverage=False,
    x_array_name=None,
    x_dtype=None,
    y_array_name=None,
    y_dtype=None,
    num_subdivisions=15,
):
    """A child process for running the DEM validation in parallel.

    It takes the input_height (m) and the dem_indices (flattened), as well
    as a duplexed multiprocessing.connection.Connection object (i.e. an open pipe)
    for processing it. It reads the arrays into local memory, then uses the connection
    to pass data back and forth until getting a "STOP" command over the connection.

    'measure_coverage' is a boolean parameter to measure how well a given pixel is covered by ICESat-2 photons.
    We'll measure a couple of different measures (centrality and coverage), and insert those parameters in the output.

    'min_photons' is the fewest photons a grid cell may contain and still be validated. Cells with fewer than
    this many photons are omitted from the returned dataframe entirely; nothing is reported for them.

    Cells with at least INTERDECILE_MIN_PHOTONS photons have their outliers trimmed to the interdecile range
    before their statistics are computed. Cells below that use every photon they contain.
    """
    # Define shared memory arrays here.
    h_shm = shared_memory.SharedMemory(name=height_array_name)
    heights = numpy.ndarray(array_shape, dtype=height_dtype, buffer=h_shm.buf)

    pi_shm = shared_memory.SharedMemory(name=i_array_name)
    photon_i = numpy.ndarray(  # noqa: F841 (used by name inside numexpr.evaluate() below)
        array_shape,
        dtype=i_dtype,
        buffer=pi_shm.buf,
    )

    pj_shm = shared_memory.SharedMemory(name=j_array_name)
    photon_j = numpy.ndarray(  # noqa: F841 (used by name inside numexpr.evaluate() below)
        array_shape,
        dtype=j_dtype,
        buffer=pj_shm.buf,
    )

    pc_shm = shared_memory.SharedMemory(name=code_array_name)
    ph_codes = numpy.ndarray(array_shape, dtype=code_dtype, buffer=pc_shm.buf)

    if measure_coverage:
        x_shm = shared_memory.SharedMemory(name=x_array_name)
        ph_x = numpy.ndarray(array_shape, dtype=x_dtype, buffer=x_shm.buf)

        y_shm = shared_memory.SharedMemory(name=y_array_name)
        ph_y = numpy.ndarray(array_shape, dtype=y_dtype, buffer=y_shm.buf)
    else:
        x_shm = None
        y_shm = None
        ph_x = None
        ph_y = None

    # Just keep looping and checking the connection pipe. When we get
    # a stop command, return from the function.
    while True:
        if connection.poll():
            if measure_coverage:
                # If we're measuring the coverage, also give us the bounding boxes of the grid cells
                (
                    dem_i_list,
                    dem_j_list,
                    dem_elev_list,
                    cell_xmin_list,
                    cell_xmax_list,
                    cell_ymin_list,
                    cell_ymax_list,
                ) = connection.recv()

            else:
                dem_i_list, dem_j_list, dem_elev_list = connection.recv()

                cell_xmin_list = None
                cell_ymin_list = None
                cell_xmax_list = None
                cell_ymax_list = None

            # Upon the "STOP" mesage, break the loop, close the shared memory objects, and return.
            if (type(dem_i_list) is str) and (dem_i_list == "STOP"):
                h_shm.close()
                pi_shm.close()
                pj_shm.close()
                pc_shm.close()
                if measure_coverage:
                    x_shm.close()
                    y_shm.close()
                return

            assert len(dem_i_list) == len(dem_j_list)
            N = len(dem_i_list)

            # Do the work.
            # r_keep marks the cells that had enough photons to validate. Cells left
            # False are dropped from the results below, not reported as empty.
            r_keep = numpy.zeros((N,), dtype=bool)
            r_mean = numpy.zeros((N,), dtype=float)
            r_numphotons = numpy.zeros((N,), dtype=numpy.uint32)
            r_numphotons_bathy = r_numphotons.copy()
            r_numphotons_intd = r_numphotons.copy()
            r_std = numpy.zeros((N,), dtype=float)
            r_interdecile = numpy.zeros((N,), float)
            r_range = numpy.zeros((N,), heights.dtype)
            r_10p = numpy.zeros((N,), float)
            r_90p = numpy.zeros((N,), float)
            r_dem_elev = numpy.zeros((N,), dtype=float)
            r_mean_diff = numpy.zeros((N,), dtype=float)
            if measure_coverage:
                r_coverage_frac = numpy.zeros((N,), dtype=float)
            else:
                r_coverage_frac = None

            # 'i' and 'j' look unused, but numexpr.evaluate() resolves the names in
            # its expression string against this frame's locals, so they are read
            # below and must keep these names.
            for counter, (i, j) in enumerate(  # noqa: B007
                zip(dem_i_list, dem_j_list, strict=True),
            ):
                # Using numexpr.evaluate here is far more memory-and-time efficient than just doing it with the numpy arrays.
                ph_subset_mask = numexpr.evaluate("(photon_i == i) & (photon_j == j)")
                # Generate a small pandas dataframe from the subset
                subset_df = pandas.DataFrame(
                    {
                        "height": heights[ph_subset_mask],
                        "ph_code": ph_codes[ph_subset_mask],
                    },
                )

                # Define and compute measures of centrality & coverage here.
                if measure_coverage:
                    # Add the x and y coords to the dataframe
                    subset_df["xcoord"] = ph_x[ph_subset_mask]
                    subset_df["ycoord"] = ph_y[ph_subset_mask]

                    cell_xmin = cell_xmin_list[counter]
                    cell_xmax = cell_xmax_list[counter]
                    cell_ymin = cell_ymin_list[counter]
                    cell_ymax = cell_ymax_list[counter]
                    assert (cell_xmax > cell_xmin) and (cell_ymax > cell_ymin)

                    cell_xstep = (cell_xmax - cell_xmin) / num_subdivisions
                    # Equal to the geotransform, the y-value starts at the top (max) and iterate downward (negative step.)
                    cell_ystep = (cell_ymin - cell_ymax) / num_subdivisions

                    assert (cell_xstep > 0) and (cell_ystep < 0)

                    subset_df["subset_i"] = numpy.floor(
                        (subset_df.ycoord - cell_ymax) / cell_ystep,
                    ).astype(int)
                    subset_df["subset_j"] = numpy.floor(
                        (subset_df.xcoord - cell_xmin) / cell_xstep,
                    ).astype(int)

                    # By taking i * (number_of_rows) + j, we come up with unique single values for the sub-cell this is in.
                    subset_df["subset_ij"] = (
                        subset_df.subset_i * num_subdivisions
                    ) + subset_df.subset_j
                    # Count how many unique subset-cells are covered and divide by the number of total sub-cells.
                    cell_fraction_covered = len(subset_df.subset_ij.unique()) / (
                        num_subdivisions**2
                    )
                    r_coverage_frac[counter] = cell_fraction_covered

                # After calculating the coverage, if we want to limit the number of photons we're dealing with total,
                # do it here.
                if photon_limit is not None and len(subset_df) > photon_limit:
                    assert photon_limit >= 2
                    subset_df = subset_df.sample(n=photon_limit)

                n_photons = len(subset_df)

                # Cells without enough photons to validate are omitted from the
                # results entirely. Leaving r_keep False drops them below.
                if n_photons < min_photons:
                    continue

                # The photon classes have already been filtered upstream (in
                # _compute_photon_overlap), so every photon here is one the user
                # asked to validate against.
                r_keep[counter] = True
                r_numphotons[counter] = n_photons
                r_dem_elev[counter] = dem_elev_list[counter]
                r_numphotons_bathy[counter] = numpy.count_nonzero(
                    subset_df.ph_code == 40,
                )
                r_range[counter] = subset_df.height.max() - subset_df.height.min()

                if n_photons >= INTERDECILE_MIN_PHOTONS:
                    # Enough photons to tell signal from outliers: keep only those
                    # within the interdecile range and compute the stats on those.
                    height_desc = subset_df.height.describe(
                        percentiles=[0.10, 0.90],
                    )
                    zp10 = height_desc["10%"]
                    zp90 = height_desc["90%"]
                    r_10p[counter], r_90p[counter] = zp10, zp90
                    r_interdecile[counter] = zp90 - zp10
                    heights_used = subset_df.height[
                        (subset_df.height >= zp10) & (subset_df.height <= zp90)
                    ]
                else:
                    # Too few photons for an interdecile range to mean anything, so
                    # use all of them: a lone photon's height becomes the cell mean,
                    # and 2-4 photons are simply averaged.
                    r_10p[counter] = EMPTY_VAL
                    r_90p[counter] = EMPTY_VAL
                    r_interdecile[counter] = EMPTY_VAL
                    heights_used = subset_df.height

                r_numphotons_intd[counter] = len(heights_used)
                r_mean[counter] = heights_used.mean()
                # A single photon has no spread to measure; pandas returns NaN here.
                r_std[counter] = heights_used.std()
                r_mean_diff[counter] = dem_elev_list[counter] - r_mean[counter]

            # Generate a little dataframe of the outputs for the grid cells that had
            # enough photons to validate. Cells below 'min_photons' were skipped in
            # the loop above and are omitted here rather than reported as empty.
            results_df = pandas.DataFrame(
                {
                    "i": numpy.asarray(dem_i_list)[r_keep],
                    "j": numpy.asarray(dem_j_list)[r_keep],
                    "mean": r_mean[r_keep],
                    "stddev": r_std[r_keep],
                    "numphotons": r_numphotons[r_keep],
                    "numphotons_bathy": r_numphotons_bathy[r_keep],
                    "numphotons_intd": r_numphotons_intd[r_keep],
                    "interdecile_range": r_interdecile[r_keep],
                    "range": r_range[r_keep],
                    "10p": r_10p[r_keep],
                    "90p": r_90p[r_keep],
                    # "canopy_fraction": r_canopy_fraction,
                    "dem_elev": r_dem_elev[r_keep],
                    "diff_mean": r_mean_diff[r_keep],
                },
            ).set_index(["i", "j"])

            if measure_coverage:
                # Add columns for centrality measurements here.
                # results_df["min_dist_from_center"] = r_min_distance_to_center
                results_df["coverage_frac"] = r_coverage_frac[r_keep]

            connection.send(results_df)


def clean_procs_and_pipes(procs, pipes1, pipes2, memory_objs):
    """Join all processes and close all pipes.

    Useful for cleaning up after multiprocessing.
    """
    # Close up all processes.
    for pr in procs:
        if isinstance(pr, mp.Process):
            if pr.is_alive():
                pr.kill()
            pr.join()

    # Close all pipes.
    for p1 in pipes1:
        if isinstance(p1, mp.connection.Connection):
            p1.close()
    for p2 in pipes2:
        if isinstance(p2, mp.connection.Connection):
            p2.close()

    # Clean up shared memory objoects.
    for smo in memory_objs:
        smo.close()
        with contextlib.suppress(FileNotFoundError):
            smo.unlink()


def kick_off_new_child_process(
    height_array_name,
    height_dtype,
    i_array_name,
    i_dtype,
    j_array_name,
    j_dtype,
    code_array_name,
    code_dtype,
    array_shape,
    photon_limit=None,
    min_photons=3,
    measure_coverage=False,
    x_array_name=None,
    x_dtype=None,
    y_array_name=None,
    y_dtype=None,
    num_subdivisions=15,
):
    """Start a new subprocess to handle and process data."""
    pipe_parent, pipe_child = mp.Pipe(duplex=True)
    proc = mp.Process(
        target=validate_dem_child_process,
        args=(
            height_array_name,
            height_dtype,
            i_array_name,
            i_dtype,
            j_array_name,
            j_dtype,
            code_array_name,
            code_dtype,
            array_shape,
            pipe_child,
        ),
        kwargs={
            "measure_coverage": measure_coverage,
            "x_array_name": x_array_name,
            "x_dtype": x_dtype,
            "y_array_name": y_array_name,
            "y_dtype": y_dtype,
            "photon_limit": photon_limit,
            "min_photons": min_photons,
            "num_subdivisions": num_subdivisions,
        },
    )
    proc.start()
    return proc, pipe_parent, pipe_child


def subdivide_dem(
    dem_name: str,
    factor: int = 2,
    output_dir: str | None = None,
    verbose: bool = False,
) -> list[str]:
    """Split a DEM into 4 smaller parts."""
    if not os.path.exists(dem_name):
        raise FileNotFoundError(f"DEM {dem_name} does not exist.")

    return ivert.utils.split_dem.split(
        dem_name,
        factor=factor,
        output_dir=output_dir,
        verbose=verbose,
    )


def reset_results_indexes_after_merge(
    sub_results_df: pandas.DataFrame,
    sub_dem_fname: str,
    parent_dem_fname: str,
) -> pandas.DataFrame:
    """DEM results dataframes are indexed by (i, j).  Reset the index after merging."""
    if (
        "i" not in sub_results_df.columns and "i" not in sub_results_df.index.names
    ) or ("j" not in sub_results_df.columns and "j" not in sub_results_df.index.names):
        raise ValueError(
            "sub_results_df must have columns 'i' and 'j' in columns or index.",
        )

    with rasterio.open(sub_dem_fname) as sub_ds:
        sub_geotransform = sub_ds.transform.to_gdal()
    with rasterio.open(parent_dem_fname) as parent_ds:
        parent_geotransform = parent_ds.transform.to_gdal()

    x_step = sub_geotransform[1]
    y_step = sub_geotransform[5]

    # The two DEMs should have the exact same x- and y-steps (resolutions).
    if x_step != parent_geotransform[1] or y_step != parent_geotransform[5]:
        raise ValueError(
            f"DEMs {os.path.basename(sub_dem_fname)} and {os.path.basename(parent_dem_fname)}"
            " have different x- or y-resolutions. Cannot combine results.",
        )

    x_offset = int((sub_geotransform[0] - parent_geotransform[0]) / x_step)
    y_offset = int((sub_geotransform[3] - parent_geotransform[3]) / y_step)

    # Assign a dem (i,j) index location for each grid cell, as new columns.
    sub_results_df["i"] = sub_results_df.index.get_level_values("i") + y_offset
    sub_results_df["j"] = sub_results_df.index.get_level_values("j") + x_offset

    # Re-create an (i,j) multi-index into the array, dropping the old index and the new columns.
    sub_results_df.set_index(["i", "j"], drop=True, inplace=True)

    return sub_results_df


def validate_dem(
    dem_name: str,
    output_dir: str | None = None,
    dates: list[int, int] | tuple[int, int] | None = None,
    classes: list[int] | tuple[int, ...] = (1, 6, 40),
    shared_ret_values: dict | None = None,
    icesat2_photon_database_obj: ivert.icesat2_database_v2.IS2Database | None = None,
    band_num: int = 1,
    dem_vertical_datum: str | int | None = None,
    dem_ndv: float | None = None,
    interim_data_dir: str | None = None,
    overwrite: bool = False,
    delete_datafiles: bool = False,
    write_summary_stats: bool = True,
    outliers_sd_threshold: float | None = 2.5,
    include_photon_level_validation: bool = False,
    plot_results: bool = True,
    location_name: str | None = None,
    mark_empty_results: bool = True,
    measure_coverage: bool = False,
    min_coverage_pct: float | None = None,
    max_photons_per_cell: int | None = None,
    min_photons_per_cell: int = 3,
    numprocs: int = parallel_funcs.physical_cpu_count(),
    max_subdivides: int = 4,
    subdivision_number: int = 0,
    orig_dem_name: str | None = None,
    min_confidence_level: int = 4,
    min_bathy_confidence: float = 0.90,
    export_error_formats: str | list | None = None,
    exclude_zones: list | None = None,
    verbose: bool = True,
):
    """Validate a DEM and produce output results.

    Most of this work is done in validate_dem_parallel. This function is a wrapper that calls validate_dem_parallel as
    a sub-function and tests whether it dies because of RAM limitations. If that happens, sub-divide the DEM in quarters
    and re-try, to a max recursion depth of max_subdivides.

    Args:
        dem_name (str): Name of the DEM file to validate.
        output_dir (str): Output directory for results.
        dates (None, list, tuple): 2-tuple of photon dates (mutually inclusive) for ICESat-2 data to use in this
            validation. Default: use all dates available in the database.
        classes (list, tuple): The ICESat-2 photon classes to use for validation. Photons in any other
            class are dropped before any statistics are computed. Default: [1, 6, 40], meaning ground (1),
            land ice (6), and bathy floor (40). Run 'ivert classes' for the full list of codes.
        shared_ret_values (dict, None): Shared return values from validate_dem_parallel. This is an analagous way to get
            the return values back from the calling function if this is called as a sub-process.
        icesat2_photon_database_obj (icesat2_database_v2.IS2Database): icesat-2 photon database object. Only
            used if we've already created one, such as in validate_dem_collection, for efficiency.
            Typically ignored for a single DEM validation.
        band_num (int): The raster band to use in the DEMs. 1-indexed. Defaults to 1 (first band).
        dem_vertical_datum: (str, int): The vertical datum of the DEM. Defaults to "egm2008".
        interim_data_dir (str): Output directory for intermediate data. Defaults to the same as the output_dir.
        overwrite (bool): Overwrite existing files.
        delete_datafiles (bool): Delete intermediate data files after validation is complete.
        write_summary_stats (bool): Write summary statistics of results to a textfile.
        outliers_sd_threshold (float): Threshold for outlier detection in errors. Defaults to 2.5.
        include_photon_level_validation (bool): Include photon level validation (not just cell-level validation).
        plot_results (bool): Plot results.
        location_name (str): Name of the location being validated.
        mark_empty_results (bool): Mark results that are empty in an "_EMPTY.txt" file.
        measure_coverage (bool): Measure the coverage of ICESat-2 photons within each grid-cell.
        min_coverage_pct (float): If set, drop grid cells whose measured coverage is below this
            percentage (0-100) from the validation results, stats, and plots. Requires
            measure_coverage=True (coverage must be measured to filter on it). Defaults to None
            (no coverage filtering).
        max_photons_per_cell (int): Maximum number of photons per cell.
        min_photons_per_cell (int): Minimum number of photons a grid cell must contain to be
            validated. Cells with fewer photons are omitted from the results entirely. Cells with
            at least INTERDECILE_MIN_PHOTONS photons have their outliers trimmed to the interdecile
            range before their statistics are computed; cells below that use every photon they
            contain. Defaults to 3.
        numprocs (int): Number of processes to use for parallelized validation.
        max_subdivides (int): Maximum number of times to subdivide the DEM in quarters before giving up.
        subdivision_number (int): The current recursion depth of this subdivision. Will not subdivide further if
            subdivision_number == max_subdivides.
        orig_dem_name (str): Name of the original DEM file. Only used for error messages.
        export_error_formats (str, list, None): GIS formats to export the per-cell errors into,
            as a comma-separated string or list drawn from 'tif', 'gpkg', 'shp', 'xyz'. Defaults
            to None, which uses the 'export_error_formats' config value.
        exclude_zones (list, None): Zones to exclude ICESat-2 photons from before validation.
            Each item is either a 4-value (minx, miny, maxx, maxy) bounding box in the DEM's own
            horizontal CRS, or a path to a vector file (.shp, .geojson, .gpkg) containing exclusion
            polygon(s) in any CRS. Photons falling within any zone are dropped. Defaults to None
            (no exclusions).
        verbose (bool): Be verbose.

    """
    if shared_ret_values is None:
        shared_ret_values = {}

    manager = mp.Manager()
    sub_shared_ret_values = manager.dict()

    args = (dem_name,)
    kwargs = {
        "output_dir": output_dir,
        "shared_ret_values": sub_shared_ret_values,
        "icesat2_photon_database_obj": icesat2_photon_database_obj,
        "band_num": band_num,
        "dem_vertical_datum": dem_vertical_datum,
        "dem_ndv": dem_ndv,
        "interim_data_dir": interim_data_dir,
        "overwrite": overwrite,
        "delete_datafiles": delete_datafiles,
        "dates": dates,
        "classes": classes,
        "write_summary_stats": write_summary_stats,
        "outliers_sd_threshold": outliers_sd_threshold,
        "include_photon_level_validation": include_photon_level_validation,
        "plot_results": plot_results,
        "location_name": location_name,
        "mark_empty_results": mark_empty_results,
        "measure_coverage": measure_coverage,
        "min_coverage_pct": min_coverage_pct,
        "max_photons_per_cell": max_photons_per_cell,
        "min_photons_per_cell": min_photons_per_cell,
        "numprocs": numprocs,
        "min_confidence_level": min_confidence_level,
        "min_bathy_confidence": min_bathy_confidence,
        "export_error_formats": export_error_formats,
        "exclude_zones": exclude_zones,
        "verbose": verbose,
    }

    # If we're in this from a logged process, make sure the children are logged processes as well.
    if isinstance(sys.stdout, ivert.utils.loggerproc.Logger):
        subproc = ivert.utils.loggerproc.LoggerProc(
            target=validate_dem_parallel,
            filename_out=sys.stdout.filename_out,
            output_to_terminal=sys.stdout.output_to_terminal,
            args=args,
            kwargs=kwargs,
        )
    else:
        subproc = mp.Process(target=validate_dem_parallel, args=args, kwargs=kwargs)

    subproc.start()
    subproc.join(timeout=None)
    exitcode = subproc.exitcode
    subproc.close()

    if orig_dem_name is None:
        orig_dem_name = dem_name

    if exitcode == 0:
        shared_ret_values.update(sub_shared_ret_values)

        return list(shared_ret_values.values())

    # Detect a job that was killed by the operating system (on Linux, the OOM-killer
    # sends SIGKILL, which multiprocessing reports as a negative exitcode). Windows has
    # no SIGKILL and no equivalent OOM-killer, so guard the attribute lookup: there the
    # branch simply doesn't apply and we fall through to the RuntimeError below.
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None and abs(exitcode) == abs(sigkill):
        # The job was killed by the operating system. This happens with a Memory Error. Divvy the file up and try again.

        # Unless we've already hit max recursion. In that case, error-out.
        if subdivision_number == max_subdivides:
            raise MemoryError(
                f"validate_dem.validate_dem('{orig_dem_name}', ...) was terminated, "
                f"likely due to a memory error.",
            )

        # Make sure the DEM exists that we're trying to sub-divide
        if not os.path.exists(dem_name):
            raise FileNotFoundError(
                f"validate_dem.validate_dem_parallell({orig_dem_name},...) could not find {dem_name}.",
            )

        # Split up the DEM into 4 parts.
        sub_dem_names = subdivide_dem(
            dem_name,
            factor=2,
            output_dir=output_dir,
            verbose=verbose,
        )

        sub_shared_ret_values = [manager.dict() for i in range(len(sub_dem_names))]
        assert len(sub_shared_ret_values) == len(sub_dem_names) == 4

        # Pre-read the photon database. This is easier than reading it in 4 separate times.
        if icesat2_photon_database_obj is None:
            icesat2_photon_database_obj = ivert.icesat2_database_v2.IS2Database()
            icesat2_photon_database_obj.open_gdf(verbose=verbose)

        for sub_dem_name, sub_shared_ret_dict in zip(
            sub_dem_names,
            sub_shared_ret_values,
            strict=True,
        ):
            validate_dem(
                sub_dem_name,
                output_dir=output_dir,
                shared_ret_values=dict(sub_shared_ret_dict),
                icesat2_photon_database_obj=icesat2_photon_database_obj,
                band_num=band_num,
                dem_vertical_datum=dem_vertical_datum,
                dem_ndv=dem_ndv,
                interim_data_dir=interim_data_dir,
                overwrite=overwrite,
                delete_datafiles=delete_datafiles,
                dates=dates,
                classes=classes,
                write_summary_stats=False,  # No need to write the summary stats file for subsets.
                export_error_formats=[],  # No need to export per-subset error files; done once after merge.
                outliers_sd_threshold=None,  # Don't filter outliers until we get all the results back.
                include_photon_level_validation=include_photon_level_validation,
                plot_results=False,  # Don't bother plotting the sub-results.
                location_name=location_name,
                mark_empty_results=mark_empty_results,
                measure_coverage=measure_coverage,
                min_coverage_pct=min_coverage_pct,
                max_photons_per_cell=max_photons_per_cell,
                min_photons_per_cell=min_photons_per_cell,
                numprocs=numprocs,
                min_confidence_level=min_confidence_level,
                min_bathy_confidence=min_bathy_confidence,
                exclude_zones=exclude_zones,
                verbose=verbose,
                max_subdivides=max_subdivides,
                orig_dem_name=orig_dem_name,
                subdivision_number=subdivision_number + 1,
            )

        # Now we gotta merge all the results.
        # Get a set of common keys:
        common_keys = set(
            list(sub_shared_ret_values[0].keys())
            + list(sub_shared_ret_values[1].keys())
            + list(sub_shared_ret_values[2].keys())
            + list(sub_shared_ret_values[3].keys()),
        )

        shared_results_df = None

        # First, merge the results dataframes.
        if "results_dataframe_file" in common_keys:
            common_key = "results_dataframe_file"
            all_fnames = [
                sub_shared_ret_values[i][common_key]
                for i in range(len(sub_shared_ret_values))
                if common_key in sub_shared_ret_values[i]
            ]
            # Concatenate the results dataframes.
            output_dfs = []
            for fname in all_fnames:
                dem_results_df = pandas.read_hdf(fname)
                # Now I gotta reset the i,j indexes.
                sub_dem_name = fname.replace("_results.h5", ".tif")
                parent_dem_name = dem_name
                dem_results_df = reset_results_indexes_after_merge(
                    dem_results_df,
                    sub_dem_name,
                    parent_dem_name,
                )
                output_dfs.append(dem_results_df)

            shared_results_df = pandas.concat(output_dfs, ignore_index=False, axis=0)
            # After we've combined all the resutls, *then* filter out outliers if they exist.
            if outliers_sd_threshold is not None:
                assert type(outliers_sd_threshold) in (int, float)
                diff_mean = shared_results_df["diff_mean"]
                meanval, stdval = diff_mean.mean(), diff_mean.std()
                low_cutoff = meanval - (stdval * outliers_sd_threshold)
                hi_cutoff = meanval + (stdval * outliers_sd_threshold)
                valid_mask = (diff_mean >= low_cutoff) & (diff_mean <= hi_cutoff)
                shared_results_df = shared_results_df[valid_mask].copy()
                if verbose:
                    print(
                        f"{len(shared_results_df):,} DEM cells after removing outliers.",
                    )

            output_fname = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_results.h5",
            )
            shared_results_df.to_hdf(
                output_fname,
                key="icesat2",
                complib="zlib",
                mode="w",
            )

            shared_ret_values[common_key] = output_fname

            if verbose:
                print(os.path.basename(output_fname), "written and exported.")

        # Second, export the per-cell errors from the merged dataframe.
        if export_error_formats is None:
            export_error_formats = ivert_config.export_error_formats
        if (
            export_error_formats
            and (shared_results_df is not None)
            and subdivision_number == 0
        ):
            merged_results_file = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_results.h5",
            )
            with rasterio.open(dem_name) as dem_ds_tmp:
                exported = export_error_results(
                    shared_results_df,
                    dem_ds_tmp,
                    merged_results_file,
                    export_error_formats,
                    verbose=verbose,
                )

            shared_ret_values["error_export_files"] = exported

        # If we're doing an empty results file, create one in the output directory if no results were returned.
        if (
            mark_empty_results
            and (shared_results_df is None)
            and subdivision_number == 0
        ):
            # If any of the results existed, we don't need to do this just because one sub-result doesn't exist.
            empty_fname = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_EMPTY.txt",
            )
            with open(empty_fname, "w", encoding="utf-8") as f:
                f.write(os.path.basename(dem_name) + " had no IVERT results.")
            shared_ret_values["empty_results_filename"] = empty_fname

        # Create the overall summary stats text file.
        if (
            write_summary_stats
            and shared_results_df is not None
            and subdivision_number == 0
        ):
            # Generate a new summary stats file only if we have results and if the recursion depth is zero.
            output_fname = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_summary_stats.txt",
            )
            write_summary_stats_file(shared_results_df, output_fname, verbose=verbose)
            shared_ret_values["summary_stats_filename"] = output_fname

        # Export the photon dataframe if it was called to be returned.
        if "photon_results_dataframe_file" in common_keys:
            common_key = "photon_results_dataframe_file"
            all_fnames = [
                sub_shared_ret_values[i][common_key]
                for i in range(len(sub_shared_ret_values))
                if common_key in sub_shared_ret_values[i]
            ]
            results_df = pandas.concat(
                [pandas.read_hdf(fname) for fname in all_fnames],
                ignore_index=True,
                axis=0,
            )
            output_fname = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_photons.h5",
            )
            results_df.to_hdf(output_fname, key="icesat2", complib="zlib", mode="w")
            shared_ret_values[common_key] = output_fname

        # Plot the results.
        if (
            plot_results
            and (shared_results_df is not None)
            and (subdivision_number == 0)
        ):
            output_fname = os.path.join(
                output_dir,
                os.path.splitext(os.path.basename(dem_name))[0] + "_plot.png",
            )
            ivert.plot_validation_results.plot_histogram_and_error_stats_4_panels(
                shared_results_df,
                output_fname,
                place_name=location_name,
                verbose=verbose,
            )
            shared_ret_values["plot_filename"] = output_fname

        return list(shared_ret_values.values())

    raise RuntimeError(
        f"validate_dem.validate_dem({orig_dem_name},...) exited with exitcode {exitcode}.",
    )


def get_dem_dataset_and_vars(dem_fn) -> tuple:
    """Get the rasterio dataset and the variables in the dataset.

    Return (dem_dataset, dem_array, dem_bbox, dem_step_xy).
    """
    dem_ds = rasterio.open(dem_fn)
    dem_array = dem_ds.read(1)
    gt = dem_ds.transform.to_gdal()
    dem_step_xy = (gt[1], gt[5])
    dem_bbox = (
        gt[0],
        gt[3] + (dem_ds.height + 1) * gt[5],
        gt[0] + (dem_ds.width + 1) * gt[1],
        gt[3],
    )

    return dem_ds, dem_array, dem_bbox, dem_step_xy


def _setup_output_paths(
    dem_name,
    output_dir,
    interim_data_dir,
    mark_empty_results,
    write_summary_stats,
    plot_results,
    verbose,
):
    """Create output directories and derive all output filenames.

    Returns (output_dir, interim_data_dir, results_dataframe_file,
             empty_results_filename, summary_stats_filename, plot_filename).
    """
    if not output_dir:
        output_dir = os.path.dirname(os.path.abspath(dem_name))
    if not os.path.exists(output_dir):
        if verbose:
            print("Creating output directory", output_dir)
        os.makedirs(output_dir)

    results_dataframe_file = os.path.join(
        output_dir,
        os.path.splitext(os.path.basename(dem_name))[0] + "_results.h5",
    )

    if interim_data_dir is None:
        interim_data_dir = output_dir
    if not os.path.exists(interim_data_dir):
        if verbose:
            print("Creating interim data directory", interim_data_dir)
        os.makedirs(interim_data_dir)

    empty_results_filename = ""
    if mark_empty_results:
        base, _ = os.path.splitext(results_dataframe_file)
        empty_results_filename = base + "_EMPTY.txt"

    summary_stats_filename = ""
    if write_summary_stats:
        summary_stats_filename = re.sub(
            r"_results\.h5\Z",
            "_summary_stats.txt",
            results_dataframe_file,
        )

    plot_filename = ""
    if plot_results:
        plot_filename = re.sub(r"_results\.h5\Z", "_plot.png", results_dataframe_file)

    return (
        output_dir,
        interim_data_dir,
        results_dataframe_file,
        empty_results_filename,
        summary_stats_filename,
        plot_filename,
    )


def _check_existing_outputs(
    dem_name,
    results_dataframe_file,
    empty_results_filename,
    summary_stats_filename,
    plot_filename,
    write_summary_stats,
    plot_results,
    location_name,
    overwrite,
    mark_empty_results,
    shared_ret_values,
    verbose,
    include_photon_level_validation=False,
    export_error_formats=None,
):
    """Handle overwrite deletion or early return when outputs already exist.

    Returns a files_to_export list if work is already done (caller should return it),
    or None to continue processing.
    """
    if export_error_formats is None:
        export_error_formats = ivert_config.export_error_formats

    if overwrite:
        for fn in (
            results_dataframe_file,
            summary_stats_filename,
            plot_filename,
        ):
            if fn and os.path.exists(fn):
                os.remove(fn)
        for fn in _error_export_filenames(results_dataframe_file, export_error_formats):
            if os.path.exists(fn):
                os.remove(fn)
        return None

    files_to_export = []

    if os.path.exists(results_dataframe_file):
        # Photon-level results can't be regenerated from the results dataframe alone — they require
        # the raw photon data. If they're missing, signal the caller to run the full pipeline.
        photon_results_file = ""
        if include_photon_level_validation:
            photon_results_file = _photon_results_filename(results_dataframe_file)
            if not os.path.exists(photon_results_file):
                return None

        results_dataframe = None
        files_to_export.append(results_dataframe_file)
        shared_ret_values["results_dataframe_file"] = results_dataframe_file

        if write_summary_stats:
            if not os.path.exists(summary_stats_filename):
                if results_dataframe is None:
                    if verbose:
                        print("Reading", results_dataframe_file, "...", end="")
                    results_dataframe = read_dataframe_file(results_dataframe_file)
                    if verbose:
                        print("done.")
                write_summary_stats_file(
                    results_dataframe,
                    summary_stats_filename,
                    verbose=verbose,
                )
            files_to_export.append(summary_stats_filename)
            shared_ret_values["summary_stats_filename"] = summary_stats_filename

        if export_error_formats:
            export_files = _error_export_filenames(
                results_dataframe_file,
                export_error_formats,
            )
            missing = [fn for fn in export_files if not os.path.exists(fn)]
            if missing:
                dem_ds_tmp = rasterio.open(dem_name)
                if results_dataframe is None:
                    if verbose:
                        print("Reading", results_dataframe_file, "...", end="")
                    results_dataframe = read_dataframe_file(results_dataframe_file)
                    if verbose:
                        print("done.")
                export_error_results(
                    results_dataframe,
                    dem_ds_tmp,
                    results_dataframe_file,
                    export_error_formats,
                    verbose=verbose,
                )
            files_to_export.extend(export_files)
            shared_ret_values["error_export_files"] = export_files

        if plot_results:
            if not os.path.exists(plot_filename):
                if location_name is None:
                    location_name = os.path.split(dem_name)[1]
                if results_dataframe is None:
                    if verbose:
                        print("Reading", results_dataframe_file, "...", end="")
                    results_dataframe = read_dataframe_file(results_dataframe_file)
                    if verbose:
                        print("done.")
                ivert.plot_validation_results.plot_histograms_and_line(
                    results_dataframe,
                    plot_filename,
                    place_name=location_name,
                    verbose=verbose,
                )
            files_to_export.append(plot_filename)
            shared_ret_values["plot_filename"] = plot_filename

        if photon_results_file:
            files_to_export.append(photon_results_file)
            shared_ret_values["photon_results_dataframe_file"] = photon_results_file

        if results_dataframe is None and verbose:
            print("Work already done here. Moving on.")

        return files_to_export

    if mark_empty_results and os.path.exists(empty_results_filename):
        if verbose:
            print(
                "No valid data produced during previous ICESat-2 analysis of",
                os.path.basename(dem_name) + ". Returning.",
            )
        return files_to_export

    return None


def _fetch_photons(
    dem_name,
    band_num,
    dem_vertical_datum,
    icesat2_photon_database_obj,
    dates,
    classes,
    omit_bboxes,
    verbose,
    min_confidence_level: int = 1,
    min_bathy_confidence: float = 0.75,
):
    """Open the DEM and query overlapping ICESat-2 photons.

    Returns (dem_ds, dem_array, photon_df, dem_epsg_str) or None if no photons found.
    """
    get_dem_dataset_and_vars(
        dem_name,
    )  # result unused; preserved for validation side-effects

    dem_ds = rasterio.open(dem_name)
    dem_array = dem_ds.read(band_num)

    dem_horz_ref_frame, dem_vert_ref_frame = dem_geom.get_dem_reference_frame_from_file(
        dem_name,
    )
    if dem_vertical_datum is not None:
        dem_vert_ref_frame = dem_geom.get_dem_reference_frame_from_user_input(
            dem_vertical_datum,
            "vert",
        )
    dem_epsg_str = dem_geom.get_dem_srs_string(dem_horz_ref_frame, dem_vert_ref_frame)
    dem_wgs84_bbox = dem_geom.get_wgs84_bounding_box(dem_name)

    if icesat2_photon_database_obj is None:
        icesat2_photon_database_obj = ivert.icesat2_database_v2.IS2Database()

    date_min, date_max = dates if dates is not None else (20180101, 20991231)
    dem_3d_bbox = (
        dem_wgs84_bbox[0],
        dem_wgs84_bbox[1],
        dem_wgs84_bbox[2],
        dem_wgs84_bbox[3],
        date_min,
        date_max,
    )

    photon_df = icesat2_photon_database_obj.query_photons(
        dem_3d_bbox,
        photon_classes=classes,
        omit_bboxes=omit_bboxes if omit_bboxes is not None else [],
        min_confidence_level=min_confidence_level,
        min_bathy_confidence=min_bathy_confidence,
    )

    if photon_df is None or len(photon_df) == 0:
        return None

    if verbose:
        print(
            f"{len(photon_df):,}",
            "ICESat-2 photons present in photon dataframe.",
        )

    photon_src_epsg = icesat2_photon_database_obj.get_photon_src_epsg()
    return dem_ds, dem_array, photon_df, dem_epsg_str, photon_src_epsg


def _resolve_exclude_geometry(exclude_zones, dem_epsg_str):
    """Resolve exclude-zone specs into a single shapely geometry in the DEM's horizontal CRS.

    Each item in exclude_zones is either a 4-value (minx, miny, maxx, maxy) bounding box
    already in the DEM's horizontal CRS, or a path to a vector file (.shp, .geojson, .gpkg)
    containing polygon(s) in any CRS, which get reprojected into the DEM's horizontal CRS.

    Returns a single (possibly multi-part) shapely geometry, or None if exclude_zones is empty.
    """
    dem_horz_crs = dem_geom.get_dem_reference_frame_from_user_input(dem_epsg_str, "h")

    geoms = []
    for zone in exclude_zones:
        if isinstance(zone, (list, tuple)):
            minx, miny, maxx, maxy = zone
            geoms.append(shapely.geometry.box(minx, miny, maxx, maxy))
        else:
            gdf = geopandas.read_file(zone)
            if gdf.crs is not None and not pyproj.CRS(gdf.crs).equals(dem_horz_crs):
                gdf = gdf.to_crs(dem_horz_crs)
            geoms.extend(geom for geom in gdf.geometry if geom is not None)

    if not geoms:
        return None
    return shapely.unary_union(geoms)


def _compute_photon_overlap(
    dem_ds,
    dem_array,
    photon_df,
    classes,
    dem_epsg_str,
    measure_coverage,
    verbose,
    photon_src_epsg="EPSG:4326+4979",
    cache_dir=None,
    user_ndv=None,
    exclude_zones=None,
):
    """Transform photon coordinates into DEM space and compute cell-level overlap.

    Photons whose class_code is not in 'classes' are dropped here, so every array
    handed downstream (height_field, the shared-memory arrays given to the child
    processes, the photon-level outputs) holds only the requested classes.

    Returns (photon_df, height_field, dem_overlap_i, dem_overlap_j,
             dem_overlap_elevs, N, coverage_coords) or None if no valid overlap exists.
    coverage_coords is (xmin_arr, xmax_arr, ymin_arr, ymax_arr) when measure_coverage=True, else None.
    """
    try:
        photon_df["dem_x"], photon_df["dem_y"], photon_df["dem_z"] = (
            ivert.transform_points.transform_points(
                photon_df["x"],
                photon_df["y"],
                photon_df["z"],
                src_epsg=photon_src_epsg,
                dst_epsg=dem_epsg_str,
                cache_dir=cache_dir,
            )
        )
    except (ValueError, RuntimeError):
        print("Warning: Unable to perform transformation. Using original points.")
        raise

    if exclude_zones:
        exclude_geom = _resolve_exclude_geometry(exclude_zones, dem_epsg_str)
        if exclude_geom is not None:
            excluded_mask = (
                geopandas.GeoSeries(
                    geopandas.points_from_xy(photon_df["dem_x"], photon_df["dem_y"]),
                )
                .within(exclude_geom)
                .to_numpy()
            )
            if verbose and excluded_mask.any():
                print(
                    f"{numpy.count_nonzero(excluded_mask):,}",
                    "photons excluded by exclusion zone(s).",
                )
            photon_df = photon_df[~excluded_mask]

    xstart, xstep, _, ystart, _, ystep = dem_ds.transform.to_gdal()
    photon_df["i"] = numpy.floor((photon_df["dem_y"] - ystart) / ystep).astype(int)
    photon_df["j"] = numpy.floor((photon_df["dem_x"] - xstart) / xstep).astype(int)

    photon_df = photon_df[
        (photon_df["i"] >= 0)
        & (photon_df["i"] < dem_array.shape[0])
        & (photon_df["j"] >= 0)
        & (photon_df["j"] < dem_array.shape[1])
    ]

    # Keep only the photon classes requested for this validation. Doing it here,
    # rather than inside each child process, means the height/class arrays copied
    # into shared memory carry only the photons that will actually be used.
    class_mask = numpy.isin(photon_df["class_code"], classes)
    if not class_mask.all():
        if verbose:
            print(
                f"{numpy.count_nonzero(~class_mask):,} photons dropped as outside the",
                "requested photon classes",
                f"({'/'.join(str(c) for c in classes)}).",
            )
        photon_df = photon_df[class_mask]

    if len(photon_df) == 0:
        if verbose:
            print(
                "No photons remain in the requested classes. Stopping and moving on.",
            )
        return None

    height_field = photon_df["dem_z"]

    # NDV priority: (1) user_ndv flag, (2) file header, (3) config default
    if user_ndv is not None:
        dem_ndv = user_ndv
    else:
        dem_ndv = dem_ds.nodata
        if dem_ndv is None:
            dem_ndv = EMPTY_VAL

    if numpy.isnan(dem_ndv):
        dem_goodpixel_mask = ~numpy.isnan(dem_array)
    else:
        dem_goodpixel_mask = dem_array != dem_ndv

    photon_df = photon_df.set_index(["i", "j"], drop=False)
    dem_mask_w_photons = numpy.zeros(dem_array.shape, dtype=bool)
    dem_mask_w_photons[photon_df.i, photon_df.j] = 1

    dem_overlap_mask = dem_goodpixel_mask & dem_mask_w_photons
    dem_overlap_i, dem_overlap_j = numpy.where(dem_overlap_mask)
    dem_overlap_elevs = dem_array[dem_overlap_mask]

    if verbose:
        num_goodpixels = numpy.count_nonzero(dem_goodpixel_mask)
        print(f"{num_goodpixels:,}", "land cells exist in the DEM.")
        if num_goodpixels == 0:
            print(
                "No land cells found in DEM with overlapping ICESat-2 data. Stopping and moving on.",
            )
            return None
        print(
            f"{len(photon_df):,} ICESat-2 photons overlap",
            f"{len(dem_overlap_i):,}",
            f"DEM cells ({numpy.count_nonzero(dem_overlap_mask) * 100 / num_goodpixels:0.2f}% of total DEM data).",
        )

    if numpy.count_nonzero(dem_overlap_mask) == 0:
        if verbose:
            print(
                "No overlapping ICESat-2 data with valid land cells. Stopping and moving on.",
            )
        return None

    N = len(dem_overlap_i)
    coverage_coords = None
    if measure_coverage:
        xmin_arr = xstart + (xstep * dem_overlap_j)
        xmax_arr = xmin_arr + xstep
        ymax_arr = ystart + (ystep * dem_overlap_i)
        ymin_arr = ymax_arr + ystep
        coverage_coords = (xmin_arr, xmax_arr, ymin_arr, ymax_arr)

    return (
        photon_df,
        height_field,
        dem_overlap_i,
        dem_overlap_j,
        dem_overlap_elevs,
        N,
        coverage_coords,
    )


def _run_photon_level_validation(
    photon_df,
    dem_overlap_i,
    dem_overlap_j,
    dem_overlap_elevs,
    results_dataframe_file,
    verbose,
):
    """Compute photon-level DEM minus ICESat-2 differences and write an HDF5 results file.

    'photon_df' has already been subset to the requested photon classes by
    _compute_photon_overlap. Returns the photon results file path.
    """
    if verbose:
        print("Performing photon-level validation...")
        print("\tGenerating DEM elevation dataframe... ", end="")

    dem_elev_df = pandas.DataFrame(
        {"dem_elevation": dem_overlap_elevs},
        index=pandas.MultiIndex.from_arrays(
            (dem_overlap_i, dem_overlap_j),
            names=("i", "j"),
        ),
    )
    if verbose:
        print(f"Done with {len(dem_elev_df)} records.")
        print("\tJoining photon_df and DEM elevation tables... ", end="")

    photon_df_with_dem_elevs = photon_df.join(dem_elev_df, how="left")
    photon_df_with_dem_elevs = photon_df_with_dem_elevs[
        pandas.notna(photon_df_with_dem_elevs["dem_elevation"])
    ]
    if verbose:
        print(f"Done with {len(photon_df_with_dem_elevs)} records.")
        print("\tCalculating elevation differences... ", end="")

    # Use the frame's own 'dem_z' column rather than the caller's height_field
    # Series: photon_df is (i, j)-indexed here, so an external Series would align
    # against the wrong index.
    photon_df_with_dem_elevs["dem_minus_is2_m"] = (
        photon_df_with_dem_elevs["dem_elevation"] - photon_df_with_dem_elevs["dem_z"]
    )
    if verbose:
        print("Done.")

    photon_results_dataframe_file = _photon_results_filename(results_dataframe_file)
    if verbose:
        print(
            "\tWriting",
            os.path.split(photon_results_dataframe_file)[1] + "... ",
            end="",
        )
    photon_df_with_dem_elevs.to_hdf(
        photon_results_dataframe_file,
        "icesat2",
        complib="zlib",
        complevel=3,
    )
    if verbose:
        print("Done.\n")

    return photon_results_dataframe_file


def _run_parallel_cell_validation(
    photon_df,
    height_field,
    dem_overlap_i,
    dem_overlap_j,
    dem_overlap_elevs,
    N,
    max_photons_per_cell,
    min_photons_per_cell,
    measure_coverage,
    coverage_coords,
    numprocs,
    verbose,
):
    """Run the parallel ICESat-2/DEM cell validation using child processes.

    Returns a list of per-chunk result DataFrames (possibly empty on error or no data).
    """
    if verbose:
        if max_photons_per_cell is not None:
            print(
                f"Limiting processing to {max_photons_per_cell} photons per grid cell.",
            )
        print(
            f"Validating grid cells with at least {min_photons_per_cell} photon"
            f"{'' if min_photons_per_cell == 1 else 's'}.",
        )
        print("Performing ICESat-2/DEM cell validation...")

    results_dataframes_list = []
    t_start = time.perf_counter()

    cpu_count = numprocs
    proc_id = os.getpid()
    height_array_name = f"heights_{proc_id}"
    i_array_name = f"i_{proc_id}"
    j_array_name = f"j_{proc_id}"
    code_array_name = f"codes_{proc_id}"
    assert (
        height_field.shape
        == photon_df.i.shape
        == photon_df.j.shape
        == photon_df.class_code.shape
    )

    height_smo = shared_memory.SharedMemory(
        size=height_field.nbytes,
        name=height_array_name,
        create=True,
    )
    height_smo.buf[:] = height_field.to_numpy().tobytes()
    height_dtype = height_field.dtype

    i_smo = shared_memory.SharedMemory(
        size=photon_df.i.nbytes,
        name=i_array_name,
        create=True,
    )
    i_smo.buf[:] = photon_df.i.to_numpy().tobytes()
    i_dtype = photon_df.i.dtype

    j_smo = shared_memory.SharedMemory(
        size=photon_df.j.nbytes,
        name=j_array_name,
        create=True,
    )
    j_smo.buf[:] = photon_df.j.to_numpy().tobytes()
    j_dtype = photon_df.j.dtype

    code_smo = shared_memory.SharedMemory(
        size=photon_df.class_code.nbytes,
        name=code_array_name,
        create=True,
    )
    code_smo.buf[:] = photon_df.class_code.to_numpy().tobytes()
    code_dtype = photon_df.class_code.dtype

    if measure_coverage:
        assert height_field.shape == photon_df.dem_x.shape == photon_df.dem_y.shape
        dem_overlap_xmin, dem_overlap_xmax, dem_overlap_ymin, dem_overlap_ymax = (
            coverage_coords
        )
        x_array_name = f"x_{proc_id}"
        x_smo = shared_memory.SharedMemory(
            size=photon_df.dem_x.nbytes,
            name=x_array_name,
            create=True,
        )
        x_smo.buf[:] = photon_df.dem_x.to_numpy().tobytes()
        x_dtype = photon_df.dem_x.dtype

        y_array_name = f"y_{proc_id}"
        y_smo = shared_memory.SharedMemory(
            size=photon_df.dem_y.nbytes,
            name=y_array_name,
            create=True,
        )
        y_smo.buf[:] = photon_df.dem_y.to_numpy().tobytes()
        y_dtype = photon_df.dem_y.dtype
    else:
        dem_overlap_xmin = dem_overlap_xmax = dem_overlap_ymin = dem_overlap_ymax = None
        x_array_name = y_array_name = None
        x_smo = y_smo = None
        x_dtype = y_dtype = None

    if measure_coverage:
        memory_objs = [height_smo, i_smo, j_smo, code_smo, x_smo, y_smo]
    else:
        memory_objs = [height_smo, i_smo, j_smo, code_smo]

    running_procs = [None] * cpu_count
    open_pipes_parent = [None] * cpu_count
    open_pipes_child = [None] * cpu_count
    # Cells in the chunk currently out with each child process. Progress is measured
    # in cells *handed out*, not rows handed back: a child omits any cell that fell
    # below 'min_photons_per_cell', so len(chunk_result_df) undercounts the work done
    # and the bar would stall short of N. Each pipe has at most one chunk outstanding,
    # so this is an exact count.
    chunk_sizes = [0] * cpu_count

    counter_started = 0
    counter_finished = 0
    num_chunks_started = 0
    num_chunks_finished = 0
    items_per_process_chunk = 20

    # 'disable=None' tells tqdm to draw the bar only when attached to a terminal, and
    # stay silent when the output is redirected to a file or a pipe.
    progress = tqdm.tqdm(
        total=N,
        disable=None if verbose else True,
        unit="cell",
        file=sys.stdout,
    )

    try:
        for i in range(cpu_count):
            if counter_started >= N:
                running_procs = running_procs[:i]
                open_pipes_parent = open_pipes_parent[:i]
                open_pipes_child = open_pipes_child[:i]
                chunk_sizes = chunk_sizes[:i]
                break

            running_procs[i], open_pipes_parent[i], open_pipes_child[i] = (
                kick_off_new_child_process(
                    height_array_name,
                    height_dtype,
                    i_array_name,
                    i_dtype,
                    j_array_name,
                    j_dtype,
                    code_array_name,
                    code_dtype,
                    height_field.shape,
                    photon_limit=max_photons_per_cell,
                    min_photons=min_photons_per_cell,
                    measure_coverage=measure_coverage,
                    x_array_name=x_array_name,
                    x_dtype=x_dtype,
                    y_array_name=y_array_name,
                    y_dtype=y_dtype,
                )
            )

            counter_chunk_end = min(counter_started + items_per_process_chunk, N)
            if measure_coverage:
                open_pipes_parent[i].send(
                    (
                        dem_overlap_i[counter_started:counter_chunk_end],
                        dem_overlap_j[counter_started:counter_chunk_end],
                        dem_overlap_elevs[counter_started:counter_chunk_end],
                        dem_overlap_xmin[counter_started:counter_chunk_end],
                        dem_overlap_xmax[counter_started:counter_chunk_end],
                        dem_overlap_ymin[counter_started:counter_chunk_end],
                        dem_overlap_ymax[counter_started:counter_chunk_end],
                    ),
                )
            else:
                open_pipes_parent[i].send(
                    (
                        dem_overlap_i[counter_started:counter_chunk_end],
                        dem_overlap_j[counter_started:counter_chunk_end],
                        dem_overlap_elevs[counter_started:counter_chunk_end],
                    ),
                )
            chunk_sizes[i] = counter_chunk_end - counter_started
            counter_started = counter_chunk_end
            num_chunks_started += 1

        while num_chunks_finished < num_chunks_started:
            for i, (proc, pipe, pipe_child) in enumerate(
                zip(running_procs, open_pipes_parent, open_pipes_child, strict=True),
            ):
                if proc is None:
                    continue

                if not proc.is_alive():
                    if verbose:
                        progress.write(
                            "Sub-process terminated unexpectedly. Some data may be missing. Restarting a new process.",
                            file=sys.stdout,
                        )
                    proc.join()
                    pipe.close()
                    pipe_child.close()
                    proc, pipe, pipe_child = kick_off_new_child_process(
                        height_array_name,
                        height_dtype,
                        i_array_name,
                        i_dtype,
                        j_array_name,
                        j_dtype,
                        code_array_name,
                        code_dtype,
                        height_field.shape,
                        photon_limit=max_photons_per_cell,
                        min_photons=min_photons_per_cell,
                        measure_coverage=measure_coverage,
                        x_array_name=x_array_name,
                        x_dtype=x_dtype,
                        y_array_name=y_array_name,
                        y_dtype=y_dtype,
                    )
                    running_procs[i] = proc
                    open_pipes_parent[i] = pipe
                    open_pipes_child[i] = pipe_child
                    # The chunk this child was working on died with it and is never
                    # retried, so count it as accounted for. Otherwise the bar could
                    # never reach N once a child has crashed.
                    counter_finished += chunk_sizes[i]
                    chunk_sizes[i] = 0
                    num_chunks_finished += 1
                    progress.update(counter_finished - progress.n)

                if pipe.poll():
                    chunk_result_df = pipe.recv()
                    counter_finished += chunk_sizes[i]
                    chunk_sizes[i] = 0
                    num_chunks_finished += 1
                    results_dataframes_list.append(chunk_result_df)
                    progress.update(counter_finished - progress.n)

                    if counter_started < N:
                        counter_chunk_end = min(
                            counter_started + items_per_process_chunk,
                            N,
                        )
                        if measure_coverage:
                            pipe.send(
                                (
                                    dem_overlap_i[counter_started:counter_chunk_end],
                                    dem_overlap_j[counter_started:counter_chunk_end],
                                    dem_overlap_elevs[
                                        counter_started:counter_chunk_end
                                    ],
                                    dem_overlap_xmin[counter_started:counter_chunk_end],
                                    dem_overlap_xmax[counter_started:counter_chunk_end],
                                    dem_overlap_ymin[counter_started:counter_chunk_end],
                                    dem_overlap_ymax[counter_started:counter_chunk_end],
                                ),
                            )
                        else:
                            pipe.send(
                                (
                                    dem_overlap_i[counter_started:counter_chunk_end],
                                    dem_overlap_j[counter_started:counter_chunk_end],
                                    dem_overlap_elevs[
                                        counter_started:counter_chunk_end
                                    ],
                                ),
                            )
                        chunk_sizes[i] = counter_chunk_end - counter_started
                        counter_started = counter_chunk_end
                        num_chunks_started += 1
                    else:
                        if measure_coverage:
                            pipe.send(("STOP", None, None, None, None, None, None))
                        else:
                            pipe.send(("STOP", None, None))
                        proc.join()
                        pipe.close()
                        pipe_child.close()
                        running_procs[i] = None
                        open_pipes_parent[i] = None
                        open_pipes_child[i] = None

    except Exception as e:
        if verbose:
            print("\nException encountered in ICESat-2 processing loop. Exiting.")
        clean_procs_and_pipes(
            running_procs,
            open_pipes_parent,
            open_pipes_child,
            memory_objs,
        )
        print(e)
        return results_dataframes_list

    finally:
        progress.close()

    t_end = time.perf_counter()
    if verbose:
        total_time_s = t_end - t_start
        if total_time_s >= 100:
            total_time_m = int(total_time_s / 60)
            partial_time_s = total_time_s % 60
            print(
                f"{total_time_m:d} minute"
                + ("s" if total_time_m > 1 else "")
                + f" {partial_time_s:0.1f} seconds total, ({(total_time_s / N) if N > 0 else 0:0.4f} s/iteration)",
            )
        else:
            print(
                f"{total_time_s:0.1f} seconds total, ({(total_time_s / N) if N > 0 else 0:0.4f} s/iteration)",
            )

    clean_procs_and_pipes(
        running_procs,
        open_pipes_parent,
        open_pipes_child,
        memory_objs,
    )
    return results_dataframes_list


def _write_validation_outputs(
    results_dataframes_list,
    dem_ds,
    dem_name,
    results_dataframe_file,
    empty_results_filename,
    summary_stats_filename,
    plot_filename,
    write_summary_stats,
    plot_results,
    location_name,
    outliers_sd_threshold,
    mark_empty_results,
    shared_ret_values,
    verbose,
    files_to_export,
    export_error_formats=None,
    min_coverage_pct=None,
):
    """Concatenate results, filter outliers, and write all output files.

    Returns the final files_to_export list.
    """
    if len(results_dataframes_list) == 0:
        return files_to_export

    results_dataframe = pandas.concat(results_dataframes_list)
    # Cells with too few photons were already dropped by the child processes, which
    # enforce 'min_photons_per_cell'. This only guards against a non-finite mean
    # arising from bad photon elevations.
    results_dataframe = results_dataframe[
        numpy.isfinite(results_dataframe["mean"])
    ].copy()

    # Drop cells below the requested minimum ICESat-2 coverage. Coverage is a
    # per-cell property, so filtering here (per subset, before any outlier removal)
    # gives the same result as filtering the merged dataframe.
    if min_coverage_pct is not None and "coverage_frac" in results_dataframe.columns:
        n_before = len(results_dataframe)
        results_dataframe = results_dataframe[
            results_dataframe["coverage_frac"] >= (min_coverage_pct / 100.0)
        ].copy()
        if verbose:
            print(
                f"{len(results_dataframe):,} of {n_before:,} DEM cells remain after applying the "
                f"{min_coverage_pct:g}% minimum-coverage filter.",
            )

    if verbose:
        print(
            "{:,} photon records used in {:,} DEM cells.".format(
                results_dataframe["numphotons_intd"].sum(),
                len(results_dataframe),
            ),
        )

    if outliers_sd_threshold is not None:
        assert type(outliers_sd_threshold) in (int, float)
        diff_mean = results_dataframe["diff_mean"]
        meanval, stdval = diff_mean.mean(), diff_mean.std()
        low_cutoff = meanval - (stdval * outliers_sd_threshold)
        hi_cutoff = meanval + (stdval * outliers_sd_threshold)
        results_dataframe = results_dataframe[
            (diff_mean >= low_cutoff) & (diff_mean <= hi_cutoff)
        ].copy()
        if verbose:
            print(
                f"{len(results_dataframe):,} DEM cells after removing outliers.",
            )

    if len(results_dataframe) == 0:
        if verbose:
            print("No valid results in results dataframe. No outputs computed.")
        if mark_empty_results:
            with open(empty_results_filename, "w", encoding="utf-8") as f:
                f.write("No ICESat-2 data data overlapping this DEM to validate.")
            if verbose:
                print(
                    "Created",
                    empty_results_filename,
                    "to indicate no data was returned here.",
                )
            files_to_export.append(empty_results_filename)
            shared_ret_values["empty_results_filename"] = empty_results_filename
        return files_to_export

    _base, ext = os.path.splitext(results_dataframe_file)
    ext = ext.lower().strip()
    if ext in (".txt", ".csv"):
        results_dataframe.to_csv(results_dataframe_file)
    else:
        results_dataframe.to_hdf(
            results_dataframe_file,
            key="icesat2",
            complib="zlib",
            mode="w",
        )
    if verbose:
        print(results_dataframe_file, "written.")
    files_to_export.append(results_dataframe_file)
    shared_ret_values["results_dataframe_file"] = results_dataframe_file

    if write_summary_stats:
        write_summary_stats_file(
            results_dataframe,
            summary_stats_filename,
            verbose=verbose,
        )
        files_to_export.append(summary_stats_filename)
        shared_ret_values["summary_stats_filename"] = summary_stats_filename

    if export_error_formats is None:
        export_error_formats = ivert_config.export_error_formats
    if export_error_formats:
        if dem_ds is None:
            dem_ds = rasterio.open(dem_name)
        exported = export_error_results(
            results_dataframe,
            dem_ds,
            results_dataframe_file,
            export_error_formats,
            verbose=verbose,
        )
        files_to_export.extend(exported)
        shared_ret_values["error_export_files"] = exported

    if plot_results:
        if location_name is None:
            location_name = os.path.split(dem_name)[1]
        ivert.plot_validation_results.plot_histograms_and_line(
            results_dataframe,
            plot_filename,
            place_name=location_name,
            figsize=(10, 4),
            verbose=verbose,
        )
        files_to_export.append(plot_filename)
        shared_ret_values["plot_filename"] = plot_filename

    return files_to_export


def validate_dem_parallel(
    dem_name: str,
    output_dir: str | None = None,
    dates: list[int, int] | tuple[int, int] | None = None,
    classes: list[int] | tuple[int, ...] = (1, 6, 40),
    shared_ret_values: dict | None = None,
    icesat2_photon_database_obj: ivert.icesat2_database_v2.IS2Database
    | None = None,  # Used only if we've already created this, for efficiency.
    band_num: int = 1,
    dem_vertical_datum: str | int | None = None,
    dem_ndv: float | None = None,
    interim_data_dir: str | None = None,
    overwrite: bool = False,
    delete_datafiles: bool = False,
    write_summary_stats: bool = True,
    outliers_sd_threshold: float = 2.5,
    include_photon_level_validation: bool = False,
    plot_results: bool = True,
    location_name: str | None = None,
    mark_empty_results: bool = True,
    omit_bboxes: list[float] | tuple[float] | None = None,
    measure_coverage: bool = False,
    min_coverage_pct: float | None = None,
    max_photons_per_cell: int | None = None,
    min_photons_per_cell: int = 3,
    numprocs: int = parallel_funcs.physical_cpu_count(),
    min_confidence_level: int = 4,
    min_bathy_confidence: float = 0.90,
    export_error_formats: str | list | None = None,
    exclude_zones: list | None = None,
    verbose: bool = True,
):
    """Validate a single DEM.

    Parameters are described above in the vdalite_dem() docstring.
    """
    if not os.path.exists(dem_name):
        raise FileNotFoundError(f"Could not find file {dem_name}.")

    if shared_ret_values is None:
        shared_ret_values = {}

    (
        output_dir,
        interim_data_dir,
        results_dataframe_file,
        empty_results_filename,
        summary_stats_filename,
        plot_filename,
    ) = _setup_output_paths(
        dem_name,
        output_dir,
        interim_data_dir,
        mark_empty_results,
        write_summary_stats,
        plot_results,
        verbose,
    )

    early = _check_existing_outputs(
        dem_name,
        results_dataframe_file,
        empty_results_filename,
        summary_stats_filename,
        plot_filename,
        write_summary_stats,
        plot_results,
        location_name,
        overwrite,
        mark_empty_results,
        shared_ret_values,
        verbose,
        include_photon_level_validation=include_photon_level_validation,
        export_error_formats=export_error_formats,
    )
    if early is not None:
        return early

    files_to_export = []

    fetch_result = _fetch_photons(
        dem_name,
        band_num,
        dem_vertical_datum,
        icesat2_photon_database_obj,
        dates,
        classes,
        omit_bboxes,
        verbose,
        min_confidence_level=min_confidence_level,
        min_bathy_confidence=min_bathy_confidence,
    )
    if fetch_result is None:
        if mark_empty_results:
            with open(empty_results_filename, "w", encoding="utf-8") as f:
                f.write(os.path.basename(dem_name) + " had no ICESat-2 results.")
            if verbose:
                print(
                    "Created",
                    empty_results_filename,
                    "to indicate no valid ICESat-2 data was returned here.",
                )
            shared_ret_values["empty_results_filename"] = empty_results_filename
            files_to_export.append(empty_results_filename)
        return files_to_export
    dem_ds, dem_array, photon_df, dem_epsg_str, photon_src_epsg = fetch_result

    overlap_result = _compute_photon_overlap(
        dem_ds,
        dem_array,
        photon_df,
        classes,
        dem_epsg_str,
        measure_coverage,
        verbose,
        photon_src_epsg=photon_src_epsg,
        cache_dir=TRANSFORMEZ_CACHE_DIR,
        user_ndv=dem_ndv,
        exclude_zones=exclude_zones,
    )
    if overlap_result is None:
        if mark_empty_results:
            with open(empty_results_filename, "w", encoding="utf-8") as f:
                f.write(os.path.basename(dem_name) + " had no ICESat-2 results.")
            if verbose:
                print(
                    "Created",
                    empty_results_filename,
                    "to indicate no data was returned here.",
                )
            shared_ret_values["empty_results_filename"] = empty_results_filename
            files_to_export.append(empty_results_filename)
        return files_to_export
    (
        photon_df,
        height_field,
        dem_overlap_i,
        dem_overlap_j,
        dem_overlap_elevs,
        N,
        coverage_coords,
    ) = overlap_result

    if include_photon_level_validation:
        photon_file = _run_photon_level_validation(
            photon_df,
            dem_overlap_i,
            dem_overlap_j,
            dem_overlap_elevs,
            results_dataframe_file,
            verbose,
        )
        files_to_export.append(photon_file)
        shared_ret_values["photon_results_dataframe_file"] = photon_file

    results_list = _run_parallel_cell_validation(
        photon_df,
        height_field,
        dem_overlap_i,
        dem_overlap_j,
        dem_overlap_elevs,
        N,
        max_photons_per_cell,
        min_photons_per_cell,
        measure_coverage,
        coverage_coords,
        numprocs,
        verbose,
    )

    return _write_validation_outputs(
        results_list,
        dem_ds,
        dem_name,
        results_dataframe_file,
        empty_results_filename,
        summary_stats_filename,
        plot_filename,
        write_summary_stats,
        plot_results,
        location_name,
        outliers_sd_threshold,
        mark_empty_results,
        shared_ret_values,
        verbose,
        files_to_export,
        export_error_formats=export_error_formats,
        min_coverage_pct=min_coverage_pct,
    )


def _format_stat(value) -> str:
    """Format a float for the summary stats file with reasonable precision.

    Uses 2 decimal places, unless the magnitude is below 0.10 (but non-zero), in
    which case it uses enough decimal places to show at least 2 significant digits.

    Args:
        value: the number to format.

    Returns:
        A string representation of the number.

    """
    x = float(value)
    if not numpy.isfinite(x):
        return str(x)
    if x != 0 and abs(x) < 0.10:
        # Enough decimals to show 2 significant digits for small magnitudes.
        decimals = 1 - int(numpy.floor(numpy.log10(abs(x))))
        return f"{x:.{decimals}f}"
    return f"{x:.2f}"


def write_summary_stats_file(
    results_df: pandas.DataFrame,
    statsfile_name: str,
    verbose: bool = True,
) -> None:
    """Write the summary statistics file.

    Args:
        results_df: pandas dataframe - contains the summary statistics
        statsfile_name: string - the name of the file to write
        verbose: bool - if True, print diagnostic messages

    Returns:
        None

    """
    if results_df is None:
        if verbose:
            print(
                "write_summary_stats_file(): No results dataframe to write. Returning",
            )
        return

    if len(results_df) == 0:
        if verbose:
            print(
                "write_summary_stats_file(): No stats to compute in results dataframe. Returning",
            )
        return

    lines = []
    lines.append(f"Number of DEM cells validated (cells): {len(results_df)}")
    lines.append(
        "Total number of ground photons used to validate this DEM (photons): {}".format(
            results_df["numphotons_intd"].sum(),
        ),
    )
    lines.append(
        "Mean number of photons used to validate each cell (photons): {}".format(
            _format_stat(results_df["numphotons_intd"].mean()),
        ),
    )

    mean_diff = results_df["diff_mean"]

    lines.append(
        f"Mean bias error (DEM - ICESat-2) (m): {_format_stat(mean_diff.mean())}",
    )
    lines.append(
        f"RMSE (m): {_format_stat(numpy.sqrt(numpy.mean(numpy.power(mean_diff, 2))))}",
    )

    lines.append(
        "Number of cells with bathymetry photons: {:d}".format(
            numpy.count_nonzero(results_df["numphotons_bathy"] > 0),
        ),
    )

    # lines.append("Mean canopy cover (% cover): {0:0.02f}".format(results_df["canopy_fraction"].mean()*100))
    # lines.append("% of cells with >0 measured canopy (%): {0}".format((numpy.count_nonzero(results_df.canopy_fraction > 0.0) / len(results_df))*100))
    # lines.append("Mean canopy cover in 'wooded' cells containing >0 canopy (% cover): {0}".format(results_df[results_df["canopy_fraction"] > 0]["canopy_fraction"].mean()*100))
    lines.append(
        "Mean roughness (stddev. of photon elevations within each cell (m)): {}".format(
            _format_stat(results_df["stddev"].mean()),
        ),
    )

    lines.append(
        "== Decile ranges of errors (DEM - ICESat-2) (m) (Look for long-tails, indicating possible artifacts.) ===",
    )

    percentile_levels = [0, 1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99, 100]
    percentile_values = numpy.percentile(mean_diff, percentile_levels)
    for level, v in zip(percentile_levels, percentile_values, strict=True):
        lines.append(f"    {level:>3d} percentile error level (m): {_format_stat(v)}")

    if "coverage_frac" in results_df.columns:
        # Rank cells by ICESat-2 coverage and report the RMSE of the best-covered
        # subsets. This shows how coverage (and thus sampling bias) affects the
        # reported DEM accuracy: each line keeps only the cells whose coverage is at
        # or above a decile threshold, from all cells (100%) to the best-covered 10%.
        coverage_frac = results_df["coverage_frac"]
        lines.append(
            "== RMSE by ICESat-2 coverage decile "
            "(how coverage/sampling bias affects reported accuracy) ===",
        )
        for pct_of_cells in range(100, 0, -10):
            # The coverage threshold that retains this fraction of the best-covered cells.
            coverage_threshold = numpy.percentile(coverage_frac, 100 - pct_of_cells)
            mask = coverage_frac >= coverage_threshold
            subset_diff = mean_diff[mask]
            rmse = numpy.sqrt(numpy.mean(numpy.power(subset_diff, 2)))
            lines.append(
                f"    RMSE for grid cells with >{coverage_threshold * 100:0.1f}% coverage ({pct_of_cells:d}% of cells) (m): {_format_stat(rmse)}",
            )

    out_text = "\n".join(lines)
    with open(statsfile_name, "w", encoding="utf-8") as outf:
        outf.write(out_text)

    if verbose:
        if os.path.exists(statsfile_name):
            print(statsfile_name, "written.")
        else:
            print(statsfile_name, "NOT written.")

    return


def generate_result_geotiff(
    results_dataframe,
    dem_ds,
    result_tif_filename,
    verbose=True,
):
    """Given the results in the dataframe, output geotiffs to visualize these.

    Name the geotiffs after the dataframe: [original_filename]_<tag>.tif

    Geotiff tags will include:
        - mean_diff
    """
    xsize, ysize = dem_ds.width, dem_ds.height
    emptyval = float(EMPTY_VAL)
    result_array = numpy.zeros([ysize, xsize], dtype=numpy.float32) + emptyval

    indices = results_dataframe.index.to_numpy()
    ivals = [idx[0] for idx in indices]
    jvals = [idx[1] for idx in indices]
    # Insert the valid values.
    result_array[ivals, jvals] = results_dataframe["diff_mean"]

    with rasterio.open(
        result_tif_filename,
        "w",
        driver="GTiff",
        width=xsize,
        height=ysize,
        count=1,
        dtype="float32",
        crs=dem_ds.crs,
        transform=dem_ds.transform,
        nodata=emptyval,
        compress="deflate",
        predictor=2,
        tiled=True,
    ) as out_ds:
        out_ds.write(result_array, 1)
    if verbose:
        print(result_tif_filename, "written.")


# Error-export formats supported by export_error_results(), selectable via the
# 'export_error_formats' config value: GeoTIFF raster, GeoPackage, ESRI Shapefile,
# and whitespace-delimited text (x y error).
ERROR_EXPORT_FORMATS = ("tif", "gpkg", "shp", "xyz")

# Curated columns written as attributes in the vector (gpkg/shp) error exports.
# Field names are kept <= 10 characters so they survive the ESRI Shapefile limit.
# (display_name, dataframe_column) pairs; columns absent from the dataframe are skipped.
_ERROR_EXPORT_FIELDS = (
    ("error", "diff_mean"),  # DEM - ICESat-2, mean per cell (the primary error value)
    ("dem_z", "dem_elev"),
    ("is2_mean", "mean"),
    ("stddev", "stddev"),
    ("n_photons", "numphotons"),
    ("n_bathy", "numphotons_bathy"),
)


def _results_cell_centers(results_dataframe, dem_ds):
    """Return (x, y) arrays of DEM-cell-center coordinates for each result row.

    Coordinates are in the DEM's own CRS, derived from the (i, j) multi-index and the
    DEM geotransform. The 0.5 offsets place each point at the center of its pixel.
    """
    gt = dem_ds.transform.to_gdal()
    indices = results_dataframe.index.to_numpy()
    ivals = numpy.array([idx[0] for idx in indices], dtype=float)
    jvals = numpy.array([idx[1] for idx in indices], dtype=float)
    x = gt[0] + (jvals + 0.5) * gt[1] + (ivals + 0.5) * gt[2]
    y = gt[3] + (jvals + 0.5) * gt[4] + (ivals + 0.5) * gt[5]
    return x, y


def _export_errors_vector(results_dataframe, dem_ds, out_fname, fmt, verbose=True):
    """Write one point per validated cell (at the cell center) to a GeoPackage or Shapefile."""
    driver_name = {"gpkg": "GPKG", "shp": "ESRI Shapefile"}[fmt]

    # Remove any previous export (including shapefile sidecar files) before writing.
    base = os.path.splitext(out_fname)[0]
    sidecar_exts = (
        (".shp", ".shx", ".dbf", ".prj", ".cpg") if fmt == "shp" else ("." + fmt,)
    )
    for ext in sidecar_exts:
        if os.path.exists(base + ext):
            os.remove(base + ext)

    fields = [
        (name, col)
        for (name, col) in _ERROR_EXPORT_FIELDS
        if col in results_dataframe.columns
    ]
    data = {}
    for name, col in fields:
        vals = results_dataframe[col].to_numpy()
        data[name] = (
            vals.astype(numpy.int32)
            if col.startswith("numphotons")
            else vals.astype(float)
        )

    x_centers, y_centers = _results_cell_centers(results_dataframe, dem_ds)
    gdf = geopandas.GeoDataFrame(
        data,
        geometry=geopandas.points_from_xy(x_centers, y_centers),
        crs=dem_ds.crs,
    )
    layer_name = os.path.splitext(os.path.basename(out_fname))[0]
    gdf.to_file(out_fname, driver=driver_name, layer=layer_name)

    if verbose:
        print(out_fname, "written.")


def _export_errors_xyz(results_dataframe, dem_ds, out_fname, verbose=True):
    """Write a whitespace-delimited 'x y error' text file, one cell-center point per line."""
    x_centers, y_centers = _results_cell_centers(results_dataframe, dem_ds)
    errors = results_dataframe["diff_mean"].to_numpy()
    numpy.savetxt(
        out_fname,
        numpy.column_stack([x_centers, y_centers, errors]),
        fmt="%.8g",
    )
    if verbose:
        print(out_fname, "written.")


def _normalize_export_formats(formats):
    """Normalize a comma-separated string or iterable of format names into a de-duplicated
    list of lower-case, recognized format names (others are dropped).
    """
    if isinstance(formats, str):
        formats = formats.split(",")
    elif formats is None:
        formats = []
    seen = []
    for f in formats:
        f = f.strip().lower().lstrip(".") if f else ""
        if f and f in ERROR_EXPORT_FORMATS and f not in seen:
            seen.append(f)
    return seen


def _photon_results_filename(results_dataframe_file):
    """Return the '<dem>_photons.h5' path matching a given results dataframe file.

    The photon output belongs next to the rest of a DEM's validation results, so
    only the file name's trailing '_results' is swapped here, never the containing
    directory. Results are written into a directory named 'ivert_results', so a
    blanket str.replace("_results", "_photons") would also rewrite the directory
    and send this file to a sibling 'ivert_photons' directory that is never created.
    """
    base, ext = os.path.splitext(results_dataframe_file)
    return base.removesuffix("_results") + "_photons" + ext


def _error_export_filenames(results_dataframe_file, formats):
    """Return the '<dem>_errors.<ext>' output paths a given format request would produce."""
    base, _ = os.path.splitext(results_dataframe_file)
    base = base.removesuffix("_results")
    base = base + "_errors"
    return [base + "." + fmt for fmt in _normalize_export_formats(formats)]


def export_error_results(
    results_dataframe,
    dem_ds,
    results_dataframe_file,
    formats,
    verbose=True,
):
    """Export the per-cell ICESat-2 errors from a results dataframe into GIS formats.

    For each requested format a single file is written next to the results dataframe,
    named '<dem>_errors.<ext>'. Supported formats (see ERROR_EXPORT_FORMATS):
        'tif'  - GeoTIFF raster of the mean error (DEM - ICESat-2) per cell.
        'gpkg' - GeoPackage of cell-center points with error attributes.
        'shp'  - ESRI Shapefile of cell-center points with error attributes.
        'xyz'  - Whitespace-delimited 'x y error' text file.

    Args:
        results_dataframe: validation results, (i, j)-multi-indexed, with a 'diff_mean' column.
        dem_ds: an open rasterio dataset for the source DEM (supplies CRS and geotransform).
        results_dataframe_file: path to the '<dem>_results.h5' file (used to derive output names).
        formats: comma-separated string (e.g. 'tif,gpkg') or iterable of format names.
        verbose: print a line per file written.

    Returns:
        list of file paths written.

    """
    exported = []
    if results_dataframe is None or len(results_dataframe) == 0:
        return exported

    formats = _normalize_export_formats(formats)
    filenames = _error_export_filenames(results_dataframe_file, formats)

    for fmt, out_fname in zip(formats, filenames, strict=True):
        if fmt == "tif":
            generate_result_geotiff(
                results_dataframe,
                dem_ds,
                out_fname,
                verbose=verbose,
            )
        elif fmt in ("gpkg", "shp"):
            _export_errors_vector(
                results_dataframe,
                dem_ds,
                out_fname,
                fmt,
                verbose=verbose,
            )
        elif fmt == "xyz":
            _export_errors_xyz(results_dataframe, dem_ds, out_fname, verbose=verbose)
        exported.append(out_fname)

    return exported


@click.command(
    help="Use ICESat-2 photon data to validate a DEM and generate statistics.",
)
@click.argument("input_dem", type=str)
@click.argument("output_dir", type=str, required=False, default="")
@click.option(
    "--classes",
    "-c",
    type=str,
    default="1/6/40",
    help="ICESat-2 photon classes to include in validation, separated by slashes. Photons in any "
    "other class are excluded before statistics are computed. Default '1/6/40', which are "
    "'ground', 'land_ice', and 'bathy_floor'.",
)
@click.option(
    "--input_vdatum",
    "-ivd",
    type=str,
    default="egm2008",
    help="Input DEM vertical datum, as a string or 'EPSG:code'. (Default: 'egm2008')",
)
@click.option(
    "--datadir",
    type=str,
    default="",
    help="A scratch directory to write interim data files. Useful if user would like to save temp files elsewhere. Defaults to the output_dir directory.",
)
@click.option(
    "--band_num",
    type=int,
    default=1,
    help="The band number (1-indexed) of the input_dem. (Default: 1)",
)
@click.option(
    "--place_name",
    "-name",
    type=str,
    default=None,
    help="A text name of the location, to put in the title of the plot (if --plot_results is selected)",
)
@click.option(
    "--numprocs",
    "-np",
    type=int,
    default=parallel_funcs.physical_cpu_count(),
    help="The number of sub-processes to run for this validation. Default to the maximum physical CPU count on this machine.",
)
@click.option(
    "--delete_datafiles",
    is_flag=True,
    default=False,
    help="Delete the interim data files generated. Reduces storage requirements. (Default: keep them all.)",
)
@click.option(
    "--measure_coverage",
    "-mc",
    is_flag=True,
    default=False,
    help="Measure the coverage %age of icesat-2 data in each of the output DEM cells.",
)
@click.option(
    "--minimum_coverage_pct",
    "-mcp",
    type=float,
    default=None,
    help="Only validate DEM grid cells whose measured coverage is at or above this "
    "percentage (0-100). Requires the -mc/--measure_coverage flag.",
)
@click.option(
    "--outlier_sd_threshold",
    default="2.5",
    help="Number of standard-deviations away from the mean to omit outliers. Default 2.5 (standard deviations). Choose 'None' if no outlier filtering is requested.",
)
@click.option(
    "--plot_results",
    is_flag=True,
    default=False,
    help="Make summary plots of the validation statistics.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite all interim and output files, even if they already exist. Default: Use interim files to compute results, saving time.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress output messaging, including error messages (just fail quietly without errors, return status 1).",
)
def main(
    input_dem,
    output_dir,
    classes,
    input_vdatum,
    datadir,
    band_num,
    place_name,
    numprocs,
    delete_datafiles,
    measure_coverage,
    minimum_coverage_pct,
    outlier_sd_threshold,
    plot_results,
    overwrite,
    quiet,
):
    """Use ICESat-2 photon data to validate a DEM and generate statistics.

    INPUT_DEM is the input DEM. OUTPUT_DIR is the directory to write output
    results; defaults to the same directory as the input filename.
    """
    if minimum_coverage_pct is not None and not measure_coverage:
        raise click.UsageError(
            "--minimum_coverage_pct requires the -mc/--measure_coverage flag "
            "(coverage must be measured before it can be filtered on).",
        )

    # The output directory defaults to the input directory.
    if not output_dir:
        output_dir = os.path.dirname(input_dem)

    # The data directory defaults to the output directory.
    if not datadir:
        datadir = output_dir

    try:
        classes_list = [int(c) for c in classes.split("/")]
    except ValueError:
        print(
            "ERROR: 'classes' must be a list of integer values separated by forward-slashes (/)",
        )
        sys.exit(1)

    # Set up multiprocessing. 'spawn' is the slowest but the most reliable. Otherwise, file handlers are fucking us up.
    # force=True avoids a RuntimeError if the start method was already set in this process.
    mp.set_start_method("spawn", force=True)

    # Run the validation
    validate_dem(
        input_dem,
        output_dir=output_dir,
        classes=classes_list,
        dem_vertical_datum=input_vdatum,
        interim_data_dir=(datadir or None),
        overwrite=overwrite,
        delete_datafiles=delete_datafiles,
        plot_results=plot_results,
        location_name=place_name,
        outliers_sd_threshold=ast.literal_eval(outlier_sd_threshold),
        measure_coverage=measure_coverage,
        min_coverage_pct=minimum_coverage_pct,
        numprocs=numprocs,
        band_num=band_num,
        verbose=not quiet,
    )


if __name__ == "__main__":
    main()
