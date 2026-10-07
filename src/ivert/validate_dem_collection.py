"""Validate and summarize an entire list or directory of DEMs."""

import datetime
import logging
import os
import re
import traceback

import numpy as np
import pandas as pd

import ivert
import ivert.bathy_filters
import ivert.icesat2_database_v2
from ivert import plot_validation_results, validate_dem
from ivert.utils import dem_source

logger = logging.getLogger(__name__)


def write_summary_csv_file(
    total_results_df_or_file: pd.DataFrame | str,
    list_of_empty_dems: list[str] | tuple[str],
    csv_name: str,
) -> pd.DataFrame:
    """Write a summary csv of all the results in a collection, after they've been run.

    Each DEM gets one row, named by its 'filename' in the results (see
    dem_source.dem_display_name()). The DEMs in 'list_of_empty_dems', named the same
    way, had no photons to validate against and get a row with no statistics.
    """
    if type(total_results_df_or_file) is str:
        total_df = pd.read_hdf(total_results_df_or_file)
    else:
        assert isinstance(total_results_df_or_file, pd.DataFrame)
        total_df = total_results_df_or_file

    if "filename" not in total_df.columns:
        msg = "total_df must have a 'filename' column."
        raise ValueError(msg)

    unique_files = total_df["filename"].unique().tolist()
    all_filenames = list(unique_files) + list(list_of_empty_dems)
    n = len(all_filenames)

    means = np.empty((n,), dtype=float)
    stds = np.empty((n,), dtype=float)
    rmses = np.empty((n,), dtype=float)
    n_cells = np.empty((n,), dtype=int)
    photons_per_cell = np.empty((n,), dtype=float)

    # Fill in the values
    for i, fname in enumerate(all_filenames):
        if fname in unique_files:
            temp_df = total_df[total_df["filename"] == fname]
            means[i] = temp_df["diff_mean"].mean()
            stds[i] = temp_df["diff_mean"].std()
            rmses[i] = (sum(temp_df["diff_mean"] ** 2) / (len(temp_df) - 1)) ** 0.5
            n_cells[i] = len(temp_df)
            photons_per_cell[i] = temp_df["numphotons_intd"].mean()

        else:
            # For files with no results, just list n/a for this.
            assert fname in list_of_empty_dems
            means[i] = np.nan
            stds[i] = np.nan
            rmses[i] = np.nan
            n_cells[i] = 0
            photons_per_cell[i] = np.nan

    output_df = pd.DataFrame(
        data={
            "filename": all_filenames,
            "rmse": rmses,
            "mean_bias": means,
            "stddev_from_mean": stds,
            "n_cells_validated": n_cells,
            "mean_photons_per_cell": photons_per_cell,
        },
    )

    output_df.to_csv(csv_name, index=False)
    logger.debug("%s written.", csv_name)

    return output_df


def _summary_results_base(place_name):
    """Return the base name (ending in '_results') of a collection's summary files."""
    # If a place name wasn't provided, just use "summary_results"
    if place_name is None:
        stats_and_plots_base = "summary_results"
    else:
        # Remove any problematic characters from the place name to create a file name.
        stats_and_plots_base = (
            place_name.replace(" ", "_")
            .replace("/", "_")
            .replace(":", "_")
            .replace("|", "_")
            .replace("\\", "_")
            .replace("?", "_")
            .replace("*", "_")
            .replace("<", "_")
            .replace(">", "_")
            .replace('"', "_")
            .replace("'", "_")
            .replace("`", "_")
            .replace("!", "_")
            .replace("@", "_")
            .replace("#", "_")
            .replace("$", "_")
            .replace("%", "_")
            .replace("^", "_")
            .replace("&", "_")
            .replace("(", "_")
            .replace(")", "_")
            .replace("+", "_")
            .replace("=", "_")
            .replace("{", "_")
            .replace("}", "_")
            .replace("[", "_")
            .replace("]", "_")
            .replace(";", "_")
            .replace(",", "_")
            .replace("/", "_")
            .replace("__", "_")
            + "_results"
        ).replace("__", "_")
    return stats_and_plots_base


# File extensions treated as DEM rasters in a collection. Anything else in a directory
# or list of files (.aux.xml, .ovr, .hdr, .prj sidecars, results files, ...) is skipped.
DEM_RASTER_EXTENSIONS = (
    ".tif",
    ".tiff",
    ".vrt",
    ".nc",
    ".nc4",
    ".img",
    ".asc",
    ".bag",
    ".grd",
    ".flt",
    ".h5",
    ".hdf5",
)

# IVERT's own HDF5 outputs, which must never be taken for DEMs if they share a directory.
_IVERT_OUTPUT_SUFFIXES = ("_results.h5", "_photons.h5")


def _is_dem_raster(fname):
    """Return True if the file name has one of the DEM_RASTER_EXTENSIONS (any case).

    IVERT's own results files ('*_results.h5', '*_photons.h5') don't count.
    """
    if fname.lower().endswith(_IVERT_OUTPUT_SUFFIXES):
        return False
    return os.path.splitext(fname)[1].lower() in DEM_RASTER_EXTENSIONS


def _log_failed_dems(failed_dems, num_dems):
    """Log, as the run's last word, which DEMs were skipped because of errors.

    Each skip was already logged when it happened. This repeats it at the end, where
    someone who didn't watch a long run will see it.
    """
    if failed_dems:
        logger.error(
            "%d of %d DEMs did not run because of errors (see the errors above): %s",
            len(failed_dems),
            num_dems,
            ", ".join(failed_dems),
        )


def _resolve_dem_list(dem_list_or_dir, fname_filter, fname_omit):
    """Return the list of DEM paths a collection validation will run over.

    No file is opened here. Each NetCDF file's variable is picked just before that DEM
    is validated, so a long run doesn't spend its start opening every file.
    """
    path = dem_list_or_dir
    # If we have a one-item list here, get the item in that list.
    if type(path) in (list, tuple) and len(path) == 1:
        path = path[0]

    if (type(path) in (list, tuple)) and (len(path) > 1):
        dem_list = [fn for fn in path if _is_dem_raster(fn)]
        skipped = [fn for fn in path if not _is_dem_raster(fn)]
        if skipped:
            logger.warning(
                "Skipping %d file(s) that are not a recognized DEM raster type (%s): %s",
                len(skipped),
                ", ".join(DEM_RASTER_EXTENSIONS),
                ", ".join(skipped),
            )
    elif os.path.isdir(path):
        dem_list = sorted(
            os.path.join(path, fname)
            for fname in os.listdir(path)
            if _is_dem_raster(fname) and os.path.isfile(os.path.join(path, fname))
        )
    else:
        assert os.path.exists(dem_source.dem_file_path(path))
        dem_list = [path]

    # Filter for needed strings in filenames, such as "_wgs84"
    if fname_filter is not None:
        # Include only filenames that MATCH the match string.
        dem_list = [fn for fn in dem_list if (re.search(fname_filter, fn) is not None)]

    # Filter out unwanted filename strings.
    if fname_omit is not None:
        # Only include filenames that DO NOT MATCH the omission string.
        dem_list = [fn for fn in dem_list if (re.search(fname_omit, fn) is None)]

    return dem_list


def _dem_output_dir(dem_path, output_dir, dem_list):
    """Return the directory a collection writes one DEM's results into.

    The DEM's own directory if 'output_dir' is None, 'output_dir' if it is an existing
    directory, and otherwise 'output_dir' taken relative to the first DEM's directory.
    """
    if output_dir is None:
        return os.path.dirname(dem_source.dem_file_path(dem_path))
    if os.path.isdir(output_dir):
        return output_dir
    return os.path.join(
        os.path.dirname(dem_source.dem_file_path(dem_list[0])),
        output_dir,
    )


# Marks a DEM that failed in a collection run, so later runs skip it until -ow is given.
ERROR_MARKER_SUFFIX = "_results_ERROR.txt"


def _error_marker_paths(dem, dem_output_dir, variable=None):
    """Return every path the error marker of this DEM (as listed) could have."""
    return [
        os.path.join(dem_output_dir, base + ERROR_MARKER_SUFFIX)
        for base in dem_source.possible_base_names(dem, variable)
    ]


def _existing_error_marker(dem, dem_output_dir, variable=None):
    """Return the path of this DEM's error marker, or None if it has none."""
    for path in _error_marker_paths(dem, dem_output_dir, variable):
        if os.path.exists(path):
            return path
    return None


def _absolute_dem_name(dem):
    """Return a DEM path or subdataset string with its file path made absolute."""
    file_path = dem_source.dem_file_path(dem)
    return dem.replace(file_path, os.path.abspath(file_path), 1)


def _write_error_marker(path, dem, message, traceback_text=None):
    """Write a DEM's error marker: the DEM, when it failed, the error, and any traceback."""
    failed_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = f"DEM: {_absolute_dem_name(dem)}\nFailed: {failed_at} (IVERT {ivert.__version__})\nError: {message}\n"
    if traceback_text:
        text += "\n" + traceback_text
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _failed_earlier(dem, dem_output_dir, include_photons=False, variable=None):
    """Return this DEM's error marker if it has one and no results, else None."""
    marker = _existing_error_marker(dem, dem_output_dir, variable)
    if marker is not None and validate_dem.dem_needs_validation(
        dem,
        dem_output_dir,
        include_photons=include_photons,
        variable=variable,
    ):
        return marker
    return None


def _dem_is_done(dem, dem_output_dir, include_photons=False, variable=None):
    """Return True if a collection run without -ow would leave this DEM alone.

    It is done if it has results, was marked empty, or failed in an earlier run.
    """
    return (
        not validate_dem.dem_needs_validation(
            dem,
            dem_output_dir,
            include_photons=include_photons,
            variable=variable,
        )
        or _existing_error_marker(dem, dem_output_dir, variable) is not None
    )


def dems_needing_validation(
    dem_list_or_dir,
    output_dir,
    include_photons=False,
    overwrite=False,
    fname_filter=None,
    fname_omit=None,
    variable=None,
):
    """Split a collection's DEMs into those validate_list_of_dems() would validate and those it would reuse.

    'output_dir' must be the absolute output directory, as 'ivert validate' passes it.
    A DEM is reused if it already has results there (see
    validate_dem.dem_needs_validation()), or failed in an earlier run and has an error
    marker, whether or not the collection's summary files exist.

    Returns:
        (to_validate, reused): two lists of DEM paths.

    """
    dem_list = _resolve_dem_list(dem_list_or_dir, fname_filter, fname_omit)
    if overwrite:
        return list(dem_list), []
    to_validate, reused = [], []
    for dem in dem_list:
        if _dem_is_done(
            dem,
            output_dir,
            include_photons=include_photons,
            variable=variable,
        ):
            reused.append(dem)
        else:
            to_validate.append(dem)
    return to_validate, reused


def validate_list_of_dems(
    dem_list_or_dir: str | list[str],
    classes: list[int] | tuple[int, ...] = (1, 6, 40),
    output_dir: str | None = None,
    fname_filter: str | None = None,
    fname_omit: str | None = None,
    band_num: int = 1,
    variable: str | None = None,
    input_vdatum: str | int | None = None,
    dem_projection: str | int | None = None,
    dem_ndv: float | None = None,
    overwrite: bool = False,
    place_name: str | None = None,
    create_individual_results: bool = True,
    include_photon_validation: bool = True,
    write_summary_csv: bool = True,
    measure_coverage: bool = False,
    min_coverage_pct: float | None = None,
    min_coverage_pct_land: float | None = None,
    min_coverage_pct_bathy: float | None = None,
    min_photons_per_cell: int = 3,
    outliers_sd_threshold: float = 2.5,
    min_confidence_level: int = 1,
    min_bathy_confidence: float = 0.75,
    bathy_filter_settings: ivert.bathy_filters.BathyFilterSettings | None = None,
    export_error_formats: str | list | None = None,
    exclude_zones: list | None = None,
):
    """Take a list of DEMs, presumably in a single area, and output validation files for those DEMs.

    DEMs should encompass a contiguous area so as to use the same set of ICESat-2 granules for
    validation. 'bathy_filter_settings' is passed to validate_dem.validate_dem(); None uses
    the 'bathy_*' config values. 'variable' picks the variable to validate in NetCDF and
    HDF5 files, as in validate_dem.validate_dem(). A file without that variable (or,
    with no variable given, without a default elevation variable) is logged and skipped.
    'dem_projection' is the DEMs' CRS, as in validate_dem.validate_dem().
    """
    if output_dir is None:
        if isinstance(dem_list_or_dir, str) and os.path.isdir(dem_list_or_dir):
            stats_and_plots_dir = dem_list_or_dir
        elif type(dem_list_or_dir) is str:
            stats_and_plots_dir = os.path.dirname(
                dem_source.dem_file_path(dem_list_or_dir),
            )
        else:
            dem_list_fitting_filter = [
                fn
                for fn in dem_list_or_dir
                if (
                    (
                        (fname_filter is None)
                        or (re.search(fname_filter, os.path.split(fn)[1]) is not None)
                    )
                    and (
                        (fname_omit is None)
                        or (re.search(fname_omit, os.path.split(fn)[1]) is None)
                    )
                )
            ]
            stats_and_plots_dir = os.path.dirname(
                dem_source.dem_file_path(dem_list_fitting_filter[0]),
            )
    elif os.path.isdir(output_dir):
        stats_and_plots_dir = output_dir
    # If the output dir appears to be a relative path, then join it with the input dir.
    elif type(dem_list_or_dir) is str:
        stats_and_plots_dir = os.path.join(
            os.path.dirname(dem_source.dem_file_path(dem_list_or_dir)),
            output_dir,
        )
    else:
        dem_list_fitting_filter = [
            fn
            for fn in dem_list_or_dir
            if (
                (
                    (fname_filter is None)
                    or (re.search(fname_filter, os.path.split(fn)[1]) is not None)
                )
                and (
                    (fname_omit is None)
                    or (re.search(fname_omit, os.path.split(fn)[1]) is None)
                )
            )
        ]
        stats_and_plots_dir = os.path.join(
            os.path.dirname(dem_source.dem_file_path(dem_list_fitting_filter[0])),
            output_dir,
        )

    stats_and_plots_base = _summary_results_base(place_name)

    # Swap only the trailing "_results" that _summary_results_base() appends. A blanket
    # str.replace("_results", ...) rewrites every occurrence, so a place name that
    # itself contains "results" (e.g. "oregon results 2024" -> "oregon_results_2024")
    # would have its own text rewritten mid-name as well.
    sibling_base = stats_and_plots_base.removesuffix("_results")

    statsfile_name = os.path.join(
        stats_and_plots_dir,
        sibling_base + "_summary_stats.txt",
    )
    plot_file_name = os.path.join(
        stats_and_plots_dir,
        sibling_base + "_plot.png",
    )
    csv_name = os.path.join(
        stats_and_plots_dir,
        sibling_base + "_individual_results.csv",
    )
    results_h5 = os.path.join(stats_and_plots_dir, stats_and_plots_base + ".h5")

    dem_list = _resolve_dem_list(dem_list_or_dir, fname_filter, fname_omit)

    # Without --overwrite, stop only if every DEM already has results (or failed in an
    # earlier run) and every summary file is written. Otherwise the loop below reuses
    # the DEMs that are done, validates the rest, and rewrites the summaries.
    summary_files = [results_h5, statsfile_name, plot_file_name]
    if write_summary_csv:
        summary_files.append(csv_name)
    if (
        not overwrite
        and all(os.path.exists(fn) for fn in summary_files)
        and all(
            _dem_is_done(
                dem,
                _dem_output_dir(dem, output_dir, dem_list),
                include_photons=include_photon_validation,
                variable=variable,
            )
            for dem in dem_list
        )
    ):
        logger.info(
            "Every DEM is already validated in %s, and the collection's summary files "
            "are written. There's nothing left to do here.\n"
            " To recompute them, run with --overwrite enabled, or delete output"
            " files as needed and re-run to create them again.",
            stats_and_plots_dir,
        )
        failed_earlier = [
            os.path.basename(dem_source.dem_file_path(dem))
            for dem in dem_list
            if _failed_earlier(
                dem,
                _dem_output_dir(dem, output_dir, dem_list),
                include_photons=include_photon_validation,
                variable=variable,
            )
        ]
        if failed_earlier:
            logger.error(
                "%d of %d DEMs failed in an earlier run and were not tried again (see "
                "their %s files; use -ow/--overwrite to retry them): %s",
                len(failed_earlier),
                len(dem_list),
                ERROR_MARKER_SUFFIX,
                ", ".join(failed_earlier),
            )
        return None

    # Generate a single photon database object and pass it repeatedly to all the objects.
    # This saves us a lot of re-reading the geodataframe repeatedly.
    photon_db_obj = ivert.icesat2_database_v2.IS2Database()
    # Read the index now, so the loaded copy is pickled into each tile's validation
    # sub-process instead of every sub-process re-reading it from disk.
    photon_db_obj.open_gdf()

    files_to_export = []
    list_of_results_dfs = []
    # The DEM behind each entry of list_of_results_dfs, in the same order. DEMs that
    # come back empty or fail are skipped, so this can't be indexed from dem_list.
    list_of_results_dems = []
    # The DEMs with no photons to validate against, as named in the summary CSV.
    list_of_empty_dems = []
    # DEMs skipped because of an error, reported again once the run is over.
    failed_dems = []

    # For each DEM, validate it.
    for i, listed_dem in enumerate(dem_list):
        logger.info(
            "\n======= %s %s of %s =======",
            os.path.split(listed_dem)[1],
            "(" + str(i + 1),
            str(len(dem_list)) + ")",
        )

        dem_file = os.path.basename(dem_source.dem_file_path(listed_dem))
        this_output_dir = _dem_output_dir(listed_dem, output_dir, dem_list)
        # '' is the current directory, for a DEM given by a bare file name.
        if this_output_dir and not os.path.exists(this_output_dir):
            os.mkdir(this_output_dir)

        # A DEM that failed in an earlier run is left alone until -ow is given.
        if overwrite:
            for marker in _error_marker_paths(listed_dem, this_output_dir, variable):
                if os.path.exists(marker):
                    os.remove(marker)
        else:
            marker = _failed_earlier(
                listed_dem,
                this_output_dir,
                include_photons=include_photon_validation,
                variable=variable,
            )
            if marker is not None:
                logger.error(
                    "Skipping %s: it failed in an earlier run (see %s). Use "
                    "-ow/--overwrite to try it again.",
                    dem_file,
                    marker,
                )
                failed_dems.append(dem_file)
                continue

        # Pick the NetCDF/HDF5 variable to validate now, one file at a time.
        try:
            dem_path = dem_source.resolve_dem_source(listed_dem, variable)
            dem_source.check_georeferenced(dem_path)
        except dem_source.DEMSourceError as exc:
            # Not logger.exception: the message names the file and what is wrong.
            logger.error("Skipping: %s", exc)  # noqa: TRY400
            _write_error_marker(
                os.path.join(
                    this_output_dir,
                    dem_source.dem_base_name(listed_dem) + ERROR_MARKER_SUFFIX,
                ),
                listed_dem,
                str(exc),
            )
            failed_dems.append(dem_file)
            continue

        results_h5_file = os.path.join(
            this_output_dir,
            dem_source.dem_base_name(dem_path) + "_results.h5",
        )
        empty_fname = results_h5_file.removesuffix("_results.h5") + "_results_EMPTY.txt"
        error_fname = results_h5_file.removesuffix("_results.h5") + ERROR_MARKER_SUFFIX

        try:
            shared_ret_values = {}
            # Do the validation.
            # Note: We automatically skip the icesat-2 download here because we already downloaded it above for the
            # whole directory.
            validate_dem.validate_dem(
                dem_path,
                output_dir,
                classes=classes,
                band_num=band_num,
                shared_ret_values=shared_ret_values,
                icesat2_photon_database_obj=photon_db_obj,
                dem_vertical_datum=input_vdatum,
                dem_projection=dem_projection,
                dem_ndv=dem_ndv,
                interim_data_dir=this_output_dir,
                overwrite=overwrite,
                write_summary_stats=create_individual_results,
                include_photon_level_validation=include_photon_validation,
                plot_results=create_individual_results,
                outliers_sd_threshold=outliers_sd_threshold,
                mark_empty_results=True,
                measure_coverage=measure_coverage,
                min_coverage_pct=min_coverage_pct,
                min_coverage_pct_land=min_coverage_pct_land,
                min_coverage_pct_bathy=min_coverage_pct_bathy,
                min_photons_per_cell=min_photons_per_cell,
                min_confidence_level=min_confidence_level,
                min_bathy_confidence=min_bathy_confidence,
                bathy_filter_settings=bathy_filter_settings,
                export_error_formats=export_error_formats,
                exclude_zones=exclude_zones,
            )
        except MemoryError:
            # Not logger.exception: running out of memory on a DEM is self-explanatory
            # and the traceback is the same every time.
            logger.error(  # noqa: TRY400
                "Skipping %s due to memory error.",
                dem_file,
            )
            _write_error_marker(
                error_fname,
                dem_path,
                "Ran out of memory validating this DEM.",
            )
            failed_dems.append(dem_file)
            continue

        except KeyboardInterrupt:
            raise

        except Exception as exc:
            logger.exception("Skipping %s.", dem_file)
            _write_error_marker(error_fname, dem_path, str(exc), traceback.format_exc())
            failed_dems.append(dem_file)
            continue

        files_to_export.extend(list(shared_ret_values.values()))

        if os.path.exists(results_h5_file):
            list_of_results_dfs.append(results_h5_file)
            list_of_results_dems.append(dem_path)

        elif os.path.exists(empty_fname):
            list_of_empty_dems.append(dem_source.dem_display_name(dem_path))

    # An extra newline is appreciated here just for readability's sake.
    logger.info("")

    if len(list_of_results_dfs) == 0:
        logger.info("No results dataframes generated. Aborting.")
        _log_failed_dems(failed_dems, len(dem_list))
        return None

    # Generate the overall summary stats file.
    total_results_df = plot_validation_results.get_data_from_h5_or_list(
        list_of_results_dfs,
        orig_filenames=list_of_results_dems,
        include_filenames=True,
    )

    # Everything appended from here on is a collection-wide summary output.
    num_tile_files = len(files_to_export)

    if write_summary_csv:
        write_summary_csv_file(
            total_results_df,
            list_of_empty_dems,
            csv_name,
        )
        files_to_export.append(csv_name)

    # The bathymetry filter counts, summed over the DEMs validated.
    bathy_filter_report = ivert.bathy_filters.BathyFilterReport.combine(
        [ivert.bathy_filters.read_report_from_h5(fn) for fn in list_of_results_dfs],
    )

    # Output the statistics summary file.
    validate_dem.write_summary_stats_file(
        total_results_df,
        statsfile_name,
        bathy_filter_report=bathy_filter_report,
    )
    files_to_export.append(statsfile_name)

    # Output the validation results plot.
    plot_validation_results.plot_histograms_and_line(
        total_results_df,
        plot_file_name,
        place_name=place_name,
    )
    files_to_export.append(plot_file_name)

    if results_h5 is not None:
        total_results_df.to_hdf(results_h5, key="results", complib="zlib", complevel=3)
        ivert.bathy_filters.write_report_to_h5(results_h5, bathy_filter_report)
        files_to_export.append(results_h5)

    validate_dem.log_written_files(files_to_export[num_tile_files:])
    _log_failed_dems(failed_dems, len(dem_list))
    return files_to_export
