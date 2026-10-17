"""Validate and summarize an entire list or directory of DEMs."""

import datetime
import logging
import re
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pandas as pd

import ivert
import ivert.bathy_filters
import ivert.icesat2_database_v2
from ivert import plot_validation_results, validate_dem
from ivert.photon_classes import SEAFLOOR
from ivert.utils import dem_source
from ivert.utils.paths import absolute_path

logger = logging.getLogger(__name__)


def write_summary_csv_file(
    total_results_df_or_file: pd.DataFrame | str,
    list_of_empty_dems: list[str] | tuple[str],
    csv_name: str | Path,
) -> pd.DataFrame:
    """Write a summary csv of all the results in a collection, after they've been run.

    Each DEM gets one row, named by its 'filename' in the results (see
    dem_source.dem_display_name()). The DEMs in 'list_of_empty_dems', named the same
    way, had no photons to validate against and get a row with no statistics.
    """
    if type(total_results_df_or_file) is str:
        total_df = pd.read_hdf(total_results_df_or_file)
    else:
        if not isinstance(total_results_df_or_file, pd.DataFrame):
            msg = "total_results_df_or_file must be a DataFrame or a file name."
            raise TypeError(msg)
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
            if fname not in list_of_empty_dems:
                msg = f"{fname} has no results and is not listed as empty."
                raise RuntimeError(msg)
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


def _summary_results_base(place_name: str | None) -> str:
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

# IVERT's own outputs with a DEM raster extension, which must never be taken for DEMs
# if they share a directory: results and photon tables, and the error rasters of
# export_error_formats' "tif" (see validate_dem._error_export_filenames()).
_IVERT_OUTPUT_SUFFIXES = ("_results.h5", "_photons.h5", "_errors.tif")


def _is_dem_raster(fname: str | Path) -> bool:
    """Return True if the file name has one of the DEM_RASTER_EXTENSIONS (any case).

    IVERT's own outputs ('*_results.h5', '*_photons.h5', '*_errors.tif') don't count.
    """
    fname = Path(fname)
    if fname.name.lower().endswith(_IVERT_OUTPUT_SUFFIXES):
        return False
    return fname.suffix.lower() in DEM_RASTER_EXTENSIONS


def _log_failed_dems(failed_dems: list[str], num_dems: int) -> None:
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


def _resolve_dem_list(
    dem_list_or_dir: str | Path | Sequence[str | Path],
    fname_filter: str | None,
    fname_omit: str | None,
) -> list[str]:
    """Return the list of DEM paths a collection validation will run over.

    No file is opened here. Each NetCDF file's variable is picked just before that DEM
    is validated, so a long run doesn't spend its start opening every file.
    """
    path = dem_list_or_dir
    # If we have a one-item list here, get the item in that list.
    if not isinstance(path, (str, Path)) and len(path) == 1:
        path = path[0]

    if not isinstance(path, (str, Path)):
        # DEM names stay strings: a NetCDF variable's name is a GDAL subdataset string.
        names = [str(fn) for fn in path]
        dem_list = [fn for fn in names if _is_dem_raster(fn)]
        skipped = [fn for fn in names if not _is_dem_raster(fn)]
        if skipped:
            logger.warning(
                "Skipping %d file(s) that are IVERT outputs or not a recognized DEM "
                "raster type (%s): %s",
                len(skipped),
                ", ".join(DEM_RASTER_EXTENSIONS),
                ", ".join(skipped),
            )
    elif Path(path).is_dir():
        # DEM names stay strings: a NetCDF variable's name is a GDAL subdataset string.
        dem_list = sorted(
            str(fpath)
            for fpath in Path(path).iterdir()
            if _is_dem_raster(fpath) and fpath.is_file()
        )
    else:
        # A str, as a DEM name: dem_source reads a subdataset string's file out of it.
        path = str(path)
        if not Path(dem_source.dem_file_path(path)).exists():
            msg = f"{path} does not exist."
            raise FileNotFoundError(msg)
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


def _dem_output_dir(dem_path: str, output_dir: str | Path | None) -> Path:
    """Return the directory a collection writes one DEM's results into.

    That is the DEM's own directory if 'output_dir' is None, and otherwise
    'output_dir', which validate_list_of_dems() has already resolved (see
    _collection_output_dir()).
    """
    if output_dir is None:
        return Path(dem_source.dem_file_path(dem_path)).parent
    return Path(output_dir)


# Marks a DEM that failed in a collection run, so later runs skip it until -ow is given.
ERROR_MARKER_SUFFIX = "_results_ERROR.txt"


def _error_marker_paths(
    dem: str,
    dem_output_dir: str | Path,
    variable: str | None = None,
) -> list[Path]:
    """Return every path the error marker of this DEM (as listed) could have."""
    return [
        Path(dem_output_dir) / (base + ERROR_MARKER_SUFFIX)
        for base in dem_source.possible_base_names(dem, variable)
    ]


def _existing_error_marker(
    dem: str,
    dem_output_dir: str | Path,
    variable: str | None = None,
) -> Path | None:
    """Return the path of this DEM's error marker, or None if it has none."""
    for path in _error_marker_paths(dem, dem_output_dir, variable):
        if path.exists():
            return path
    return None


def _absolute_dem_name(dem: str) -> str:
    """Return a DEM path or subdataset string with its file path made absolute."""
    file_path = dem_source.dem_file_path(dem)
    return dem.replace(file_path, str(absolute_path(file_path)), 1)


def _write_error_marker(
    path: str | Path,
    dem: str,
    message: str,
    traceback_text: str | None = None,
) -> None:
    """Write a DEM's error marker: the DEM, the time, the error and any traceback."""
    failed_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    text = (
        f"DEM: {_absolute_dem_name(dem)}\n"
        f"Failed: {failed_at} (IVERT {ivert.__version__})\n"
        f"Error: {message}\n"
    )
    if traceback_text:
        text += "\n" + traceback_text
    Path(path).write_text(text, encoding="utf-8")


def _failed_earlier(
    dem: str,
    dem_output_dir: str | Path,
    *,
    include_photons: bool = False,
    variable: str | None = None,
) -> Path | None:
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


def _dem_is_done(
    dem: str,
    dem_output_dir: str | Path,
    *,
    include_photons: bool = False,
    variable: str | None = None,
) -> bool:
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
    dem_list_or_dir: str | Path | Sequence[str | Path],
    output_dir: str | Path,
    *,
    include_photons: bool = False,
    overwrite: bool = False,
    fname_filter: str | None = None,
    fname_omit: str | None = None,
    variable: str | None = None,
) -> tuple[list[str], list[str]]:
    """Sort DEMs into those validate_list_of_dems() would validate or would reuse.

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


def _first_matching_dem(
    dem_names: Sequence[str | Path],
    fname_filter: str | None,
    fname_omit: str | None,
) -> str:
    """Return the first DEM name whose file name passes 'fname_filter' and 'fname_omit'.

    Only the names are checked, not whether each is a DEM raster, so this can differ
    from the first DEM that _resolve_dem_list() returns.
    """
    names = [str(dem) for dem in dem_names]
    matching = [
        fn
        for fn in names
        if (fname_filter is None or re.search(fname_filter, Path(fn).name) is not None)
        and (fname_omit is None or re.search(fname_omit, Path(fn).name) is None)
    ]
    return matching[0]


def _collection_output_dir(
    dem_list_or_dir: str | Path | Sequence[str | Path],
    output_dir: str | Path | None,
    fname_filter: str | None,
    fname_omit: str | None,
) -> Path:
    """Return the absolute directory a collection writes its summary files into.

    That is 'output_dir' if it is absolute, and otherwise the input's folder, with a
    relative 'output_dir' taken from there, never from the current directory, as
    'ivert validate' does. The input's folder is the directory given, or the folder of
    the file given, or of the first listed DEM passing the filters.
    """
    if output_dir is not None and Path(output_dir).is_absolute():
        return Path(output_dir)
    if isinstance(dem_list_or_dir, (str, Path)):
        # A str, as a DEM name: dem_source reads a subdataset string's file out of it.
        single_input = str(dem_list_or_dir)
        if Path(single_input).is_dir():
            input_dir = Path(single_input)
        else:
            input_dir = Path(dem_source.dem_file_path(single_input)).parent
    else:
        first_dem = _first_matching_dem(dem_list_or_dir, fname_filter, fname_omit)
        input_dir = Path(dem_source.dem_file_path(first_dem)).parent
    input_dir = absolute_path(input_dir)
    return input_dir if output_dir is None else input_dir / output_dir


class _SummaryFiles(NamedTuple):
    """The paths of a collection's summary files."""

    stats: Path
    plot: Path
    csv: Path
    results_h5: Path


def _summary_files(directory: Path, place_name: str | None) -> _SummaryFiles:
    """Return the paths of a collection's summary files in 'directory'."""
    results_base = _summary_results_base(place_name)
    # Swap only the trailing "_results" that _summary_results_base() appends. A blanket
    # str.replace("_results", ...) rewrites every occurrence, so a place name that
    # itself contains "results" (e.g. "oregon results 2024" -> "oregon_results_2024")
    # would have its own text rewritten mid-name as well.
    sibling_base = results_base.removesuffix("_results")
    return _SummaryFiles(
        stats=directory / (sibling_base + "_summary_stats.txt"),
        plot=directory / (sibling_base + "_plot.png"),
        csv=directory / (sibling_base + "_individual_results.csv"),
        results_h5=directory / (results_base + ".h5"),
    )


def _collection_is_finished(
    dem_list: list[str],
    output_dir: str | Path | None,
    summary: _SummaryFiles,
    *,
    write_summary_csv: bool,
    include_photons: bool,
    variable: str | None,
) -> bool:
    """Return True, and say so, if a run without -ow would have nothing left to do.

    That is when every DEM already has results (or failed in an earlier run) and every
    summary file is written. DEMs that failed in an earlier run are listed again.
    """
    summary_files = [summary.results_h5, summary.stats, summary.plot]
    if write_summary_csv:
        summary_files.append(summary.csv)
    if not all(fn.exists() for fn in summary_files) or not all(
        _dem_is_done(
            dem,
            _dem_output_dir(dem, output_dir),
            include_photons=include_photons,
            variable=variable,
        )
        for dem in dem_list
    ):
        return False

    logger.info(
        "Every DEM is already validated in %s, and the collection's summary files "
        "are written. There's nothing left to do here.\n"
        " To recompute them, run with --overwrite enabled, or delete output"
        " files as needed and re-run to create them again.",
        summary.stats.parent,
    )
    failed_earlier = [
        Path(dem_source.dem_file_path(dem)).name
        for dem in dem_list
        if _failed_earlier(
            dem,
            _dem_output_dir(dem, output_dir),
            include_photons=include_photons,
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
    return True


@dataclass
class _CollectionResults:
    """What the DEMs of a collection run produced, gathered DEM by DEM."""

    # Every file the DEMs' validations wrote, for the list of files written.
    files_to_export: list = field(default_factory=list)
    # The results file of each DEM that has one, and that DEM, in the same order.
    # DEMs that come back empty or fail are left out, so these can't be indexed from
    # the list of DEMs.
    results_files: list[str | Path] = field(default_factory=list)
    results_dems: list[str] = field(default_factory=list)
    # The DEMs with no photons to validate against, as named in the summary CSV.
    empty_dems: list[str] = field(default_factory=list)
    # DEMs skipped because of an error, reported again once the run is over.
    failed_dems: list[str] = field(default_factory=list)


def _skip_failed_earlier(
    listed_dem: str,
    dem_file: str,
    dem_output_dir: Path,
    *,
    overwrite: bool,
    include_photons: bool,
    variable: str | None,
) -> bool:
    """Return True if this DEM failed in an earlier run, and so is skipped.

    A DEM that failed is left alone until -ow is given. With 'overwrite', its error
    markers are deleted instead, and it is tried again.
    """
    if overwrite:
        for stale_marker in _error_marker_paths(listed_dem, dem_output_dir, variable):
            stale_marker.unlink(missing_ok=True)
        return False
    marker = _failed_earlier(
        listed_dem,
        dem_output_dir,
        include_photons=include_photons,
        variable=variable,
    )
    if marker is None:
        return False
    logger.error(
        "Skipping %s: it failed in an earlier run (see %s). Use "
        "-ow/--overwrite to try it again.",
        dem_file,
        marker,
    )
    return True


def _resolve_listed_dem(
    listed_dem: str,
    variable: str | None,
    dem_output_dir: Path,
) -> str | None:
    """Return the path or subdataset string to validate for a listed DEM.

    The NetCDF/HDF5 variable is picked here, one file at a time. A DEM that can't be
    read or isn't georeferenced is logged and gets an error marker; that returns None.
    """
    try:
        dem_path = dem_source.resolve_dem_source(listed_dem, variable)
        dem_source.check_georeferenced(dem_path)
    except dem_source.DEMSourceError as exc:
        # Not logger.exception: the message names the file and what is wrong.
        logger.error("Skipping: %s", exc)  # noqa: TRY400
        _write_error_marker(
            dem_output_dir
            / (dem_source.dem_base_name(listed_dem) + ERROR_MARKER_SUFFIX),
            listed_dem,
            str(exc),
        )
        return None
    return dem_path


def _run_validate_dem(
    dem_path: str,
    dem_file: str,
    error_fname: Path,
    validate_kwargs: dict[str, Any],
) -> dict | None:
    """Validate one DEM; return the files it wrote, or None if it failed.

    A failure is logged and written to the DEM's error marker, 'error_fname'.
    """
    shared_ret_values: dict = {}
    try:
        validate_dem.validate_dem(
            dem_path,
            shared_ret_values=shared_ret_values,
            **validate_kwargs,
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
        return None

    except KeyboardInterrupt:
        raise

    except Exception as exc:
        logger.exception("Skipping %s.", dem_file)
        _write_error_marker(error_fname, dem_path, str(exc), traceback.format_exc())
        return None

    return shared_ret_values


def _validate_listed_dem(
    i: int,
    dem_list: list[str],
    output_dir: str | Path | None,
    results: _CollectionResults,
    validate_kwargs: dict[str, Any],
    *,
    overwrite: bool,
    include_photons: bool,
    variable: str | None,
) -> None:
    """Validate the i-th DEM of a collection, and add what it produced to 'results'."""
    listed_dem = dem_list[i]
    logger.info(
        "\n======= %s %s of %s =======",
        Path(listed_dem).name,
        "(" + str(i + 1),
        str(len(dem_list)) + ")",
    )

    dem_file = Path(dem_source.dem_file_path(listed_dem)).name
    this_output_dir = _dem_output_dir(listed_dem, output_dir)
    this_output_dir.mkdir(parents=True, exist_ok=True)

    if _skip_failed_earlier(
        listed_dem,
        dem_file,
        this_output_dir,
        overwrite=overwrite,
        include_photons=include_photons,
        variable=variable,
    ):
        results.failed_dems.append(dem_file)
        return

    dem_path = _resolve_listed_dem(listed_dem, variable, this_output_dir)
    if dem_path is None:
        results.failed_dems.append(dem_file)
        return

    dem_base = dem_source.dem_base_name(dem_path)
    results_h5_file = this_output_dir / (dem_base + "_results.h5")
    empty_fname = this_output_dir / (dem_base + "_results_EMPTY.txt")
    error_fname = this_output_dir / (dem_base + ERROR_MARKER_SUFFIX)

    written = _run_validate_dem(dem_path, dem_file, error_fname, validate_kwargs)
    if written is None:
        results.failed_dems.append(dem_file)
        return

    results.files_to_export.extend(list(written.values()))
    if results_h5_file.exists():
        results.results_files.append(results_h5_file)
        results.results_dems.append(dem_path)
    elif empty_fname.exists():
        results.empty_dems.append(dem_source.dem_display_name(dem_path))


def _write_collection_summaries(
    results: _CollectionResults,
    summary: _SummaryFiles,
    *,
    place_name: str | None,
    write_summary_csv: bool,
) -> list:
    """Write a collection's summary files; return every file the run wrote."""
    total_results_df = plot_validation_results.get_data_from_h5_or_list(
        results.results_files,
        orig_filenames=results.results_dems,
        include_filenames=True,
    )

    files_to_export = results.files_to_export
    # Everything appended from here on is a collection-wide summary output.
    num_tile_files = len(files_to_export)

    if write_summary_csv:
        write_summary_csv_file(
            total_results_df,
            results.empty_dems,
            summary.csv,
        )
        files_to_export.append(summary.csv)

    # The bathymetry filter counts, summed over the DEMs validated.
    bathy_filter_report = ivert.bathy_filters.BathyFilterReport.combine(
        [ivert.bathy_filters.read_report_from_h5(fn) for fn in results.results_files],
    )

    # Output the statistics summary file.
    validate_dem.write_summary_stats_file(
        total_results_df,
        summary.stats,
        bathy_filter_report=bathy_filter_report,
    )
    files_to_export.append(summary.stats)

    # Output the validation results plot.
    plot_validation_results.plot_histograms_and_line(
        total_results_df,
        summary.plot,
        place_name=place_name,
    )
    files_to_export.append(summary.plot)

    total_results_df.to_hdf(
        summary.results_h5,
        key="results",
        complib="zlib",
        complevel=3,
    )
    ivert.bathy_filters.write_report_to_h5(summary.results_h5, bathy_filter_report)
    files_to_export.append(summary.results_h5)

    validate_dem.log_written_files(files_to_export[num_tile_files:])
    return files_to_export


def validate_list_of_dems(
    dem_list_or_dir: str | Path | Sequence[str | Path],
    *,
    classes: list[int] | tuple[int, ...] = (1, 6, 40),
    output_dir: str | Path | None = None,
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
) -> list | None:
    """Validate a list of DEMs, presumably in one area, and write their output files.

    DEMs should encompass a contiguous area so as to use the same set of ICESat-2
    granules for validation. 'bathy_filter_settings' is passed to
    validate_dem.validate_dem(); None uses the 'bathy_*' config values. 'variable' picks
    the variable to validate in NetCDF and HDF5 files, as in
    validate_dem.validate_dem(). A file without that variable (or, with no variable
    given, without a default elevation variable) is logged and skipped. 'dem_projection'
    is the DEMs' CRS, as in validate_dem.validate_dem().

    Every DEM's results go in 'output_dir', and so do the collection's summary files.
    A relative 'output_dir' is taken from the folder of the DEMs (see
    _collection_output_dir()), wherever Python is run from. With no 'output_dir', each
    DEM's results go beside it, and the summary files in that folder.
    """
    summary_dir = _collection_output_dir(
        dem_list_or_dir,
        output_dir,
        fname_filter,
        fname_omit,
    )
    if output_dir is not None:
        # Resolved once, so validate_dem() writes where the markers and summaries go.
        output_dir = summary_dir
    summary = _summary_files(summary_dir, place_name)
    dem_list = _resolve_dem_list(dem_list_or_dir, fname_filter, fname_omit)

    # Without --overwrite, stop only if every DEM already has results (or failed in an
    # earlier run) and every summary file is written. Otherwise the loop below reuses
    # the DEMs that are done, validates the rest, and rewrites the summaries.
    if not overwrite and _collection_is_finished(
        dem_list,
        output_dir,
        summary,
        write_summary_csv=write_summary_csv,
        include_photons=include_photon_validation,
        variable=variable,
    ):
        return None

    # Generate a single photon database object and pass it repeatedly to all the
    # objects. This saves us a lot of re-reading the geodataframe repeatedly.
    photon_db_obj = ivert.icesat2_database_v2.IS2Database()
    # Read the index now, so the loaded copy is pickled into each tile's validation
    # sub-process instead of every sub-process re-reading it from disk.
    photon_db_obj.open_gdf()
    # Each DEM is validated in a process forked from this one; loading this here,
    # once, saves each of them from loading it for itself.
    if SEAFLOOR in classes and len(dem_list) > 1:
        ivert.bathy_filters.preload_reference_fetcher(
            bathy_filter_settings
            or ivert.bathy_filters.BathyFilterSettings.from_config(),
        )

    # The settings every DEM's validate_dem.validate_dem() call gets.
    validate_kwargs: dict[str, Any] = {
        "output_dir": output_dir,
        "classes": classes,
        "band_num": band_num,
        "icesat2_photon_database_obj": photon_db_obj,
        "dem_vertical_datum": input_vdatum,
        "dem_projection": dem_projection,
        "dem_ndv": dem_ndv,
        "overwrite": overwrite,
        "write_summary_stats": create_individual_results,
        "include_photon_level_validation": include_photon_validation,
        "plot_results": create_individual_results,
        "outliers_sd_threshold": outliers_sd_threshold,
        "mark_empty_results": True,
        "measure_coverage": measure_coverage,
        "min_coverage_pct": min_coverage_pct,
        "min_coverage_pct_land": min_coverage_pct_land,
        "min_coverage_pct_bathy": min_coverage_pct_bathy,
        "min_photons_per_cell": min_photons_per_cell,
        "min_confidence_level": min_confidence_level,
        "min_bathy_confidence": min_bathy_confidence,
        "bathy_filter_settings": bathy_filter_settings,
        "export_error_formats": export_error_formats,
        "exclude_zones": exclude_zones,
    }

    results = _CollectionResults()
    for i in range(len(dem_list)):
        _validate_listed_dem(
            i,
            dem_list,
            output_dir,
            results,
            validate_kwargs,
            overwrite=overwrite,
            include_photons=include_photon_validation,
            variable=variable,
        )

    # An extra newline is appreciated here just for readability's sake.
    logger.info("")

    if not results.results_files:
        logger.info("No results dataframes generated. Aborting.")
        _log_failed_dems(results.failed_dems, len(dem_list))
        return None

    files_to_export = _write_collection_summaries(
        results,
        summary,
        place_name=place_name,
        write_summary_csv=write_summary_csv,
    )
    _log_failed_dems(results.failed_dems, len(dem_list))
    return files_to_export
