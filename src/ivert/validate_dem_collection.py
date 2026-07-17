"""validate_dem_collection.py
Code for validating and summarizing an entire list or directory of DEMs.
"""

import ast
import multiprocessing as mp
import os
import re
import traceback

import click
import numpy
import pandas

import ivert.icesat2_database_v2
import ivert.utils.query_yes_no as yes_no
from ivert import plot_validation_results, validate_dem


def write_summary_csv_file(
    total_results_df_or_file: pandas.DataFrame | str,
    list_of_empty_files: list[str] | tuple[str],
    csv_name: str,
    verbose: bool = True,
) -> pandas.DataFrame:
    """Write a summary csv of all the results in a collection, after they've been run."""
    if type(total_results_df_or_file) is str:
        total_df = pandas.read_hdf(total_results_df_or_file)
    else:
        assert isinstance(total_results_df_or_file, pandas.DataFrame)
        total_df = total_results_df_or_file

    if "filename" not in total_df.columns:
        raise ValueError("total_df must have a 'filename' column.")

    unique_files = total_df["filename"].unique().tolist()
    all_filenames = list(unique_files) + list(list_of_empty_files)
    N = len(all_filenames)

    means = numpy.empty((N,), dtype=float)
    stds = numpy.empty((N,), dtype=float)
    rmses = numpy.empty((N,), dtype=float)
    n_cells = numpy.empty((N,), dtype=int)
    photons_per_cell = numpy.empty((N,), dtype=float)
    # canopy_mean = numpy.empty((N,), dtype=float)
    # canopy_mean_gt0 = numpy.empty((N,), dtype=float)

    # Fill in the values
    for i, fname in enumerate(all_filenames):
        if fname in unique_files:
            temp_df = total_df[total_df["filename"] == fname]
            means[i] = temp_df["diff_mean"].mean()
            stds[i] = temp_df["diff_mean"].std()
            rmses[i] = (sum(temp_df["diff_mean"] ** 2) / (len(temp_df) - 1)) ** 0.5
            n_cells[i] = len(temp_df)
            photons_per_cell[i] = temp_df["numphotons_intd"].mean()
            # canopy_mean[i] = temp_df['canopy_fraction'].mean()
            # canopy_mean_gt0[i] = temp_df[temp_df['canopy_fraction'] > 0]['canopy_fraction'].mean()

        else:
            # For files with no results, just list n/a for this.
            assert fname in list_of_empty_files
            means[i] = numpy.nan
            stds[i] = numpy.nan
            rmses[i] = numpy.nan
            n_cells[i] = 0
            photons_per_cell[i] = numpy.nan
            # canopy_mean[i] = numpy.nan
            # canopy_mean_gt0[i] = numpy.nan

    output_df = pandas.DataFrame(
        data={
            "filename": all_filenames,
            "rmse": rmses,
            "mean_bias": means,
            "stddev_from_mean": stds,
            "n_cells_validated": n_cells,
            "mean_photons_per_cell": photons_per_cell,
            # "canopy_mean": canopy_mean,
            # "canopy_mean_gt0": canopy_mean_gt0
        },
    )

    output_df.to_csv(csv_name, index=False)
    if verbose:
        print(csv_name, "written.")

    return output_df


def validate_list_of_dems(
    dem_list_or_dir: str | list[str],
    classes: list[int] | tuple[int] = [1, 6, 40],
    output_dir: str | None = None,
    fname_filter: str | None = r"\.tif\Z",
    fname_omit: str | None = None,
    band_num: int = 1,
    input_vdatum: str | int | None = None,
    dem_ndv: float | None = None,
    overwrite: bool = False,
    place_name: str | None = None,
    create_individual_results: bool = True,
    delete_datafiles: bool = False,
    include_photon_validation: bool = True,
    write_result_tifs: bool = False,
    write_summary_csv: bool = True,
    measure_coverage: bool = False,
    outliers_sd_threshold: float = 2.5,
    min_confidence_level: int = 1,
    min_bathy_confidence: float = 0.75,
    export_error_formats: str | list | None = None,
    exclude_zones: list | None = None,
    verbose: bool = True,
):
    """Take a list of DEMs, presumably in a single area, and output validation files for those DEMs.

    DEMs should encompass a contiguous area so as to use the same set of ICESat-2 granules for
    validation.
    """
    if output_dir is None:
        if isinstance(dem_list_or_dir, str) and os.path.isdir(dem_list_or_dir):
            stats_and_plots_dir = dem_list_or_dir
        elif type(dem_list_or_dir) is str:
            stats_and_plots_dir = os.path.dirname(dem_list_or_dir)
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
            stats_and_plots_dir = os.path.dirname(dem_list_fitting_filter[0])
    elif os.path.isdir(output_dir):
        stats_and_plots_dir = output_dir
    # If the output dir appears to be a relative path, then join it with the input dir.
    elif type(dem_list_or_dir) is str:
        stats_and_plots_dir = os.path.join(
            os.path.dirname(dem_list_or_dir),
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
            os.path.dirname(dem_list_fitting_filter[0]),
            output_dir,
        )

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

    statsfile_name = os.path.join(
        stats_and_plots_dir,
        stats_and_plots_base.replace("_results", "_summary_stats") + ".txt",
    )
    plot_file_name = os.path.join(
        stats_and_plots_dir,
        stats_and_plots_base.replace("_results", "_plot") + ".png",
    )
    csv_name = os.path.join(
        stats_and_plots_dir,
        stats_and_plots_base.replace("_results", "_individual_results") + ".csv",
    )
    results_h5 = os.path.join(stats_and_plots_dir, stats_and_plots_base + ".h5")

    # If the .h5 results file already exists but not the other files, just
    # create them and exit.
    if (not overwrite) and (results_h5 is not None) and os.path.exists(results_h5):
        results_df = None

        if not os.path.exists(statsfile_name):
            results_df = pandas.read_hdf(results_h5)
            if verbose:
                print(results_df, "read.")
            validate_dem.write_summary_stats_file(
                results_df,
                statsfile_name,
                verbose=verbose,
            )

        if not os.path.exists(plot_file_name):
            if results_df is None:
                results_df = pandas.read_hdf(results_h5)
                if verbose:
                    print(results_df, "read.")
            plot_validation_results.plot_histograms_and_line(
                results_df,
                plot_file_name,
                place_name=place_name,
                verbose=verbose,
            )

        if (results_df is None) and verbose:
            print(
                "Files '" + results_h5 + "',",
                "'" + statsfile_name + "', and '",
                plot_file_name
                + "' are all already written. There's nothing left to do here.\n",
                "To recompute them, run with --overwrite enabled, or delete output files as needed and re-run to "
                "create them again.\n",
                "Exiting.",
            )
        return None

    path = dem_list_or_dir
    # If we have a one-item list here, get the item in that list.
    if type(path) in (list, tuple) and len(path) == 1:
        path = path[0]

    if (type(path) in (list, tuple)) and (len(path) > 1):
        dem_list = path
    elif os.path.isdir(path):
        dem_list = sorted([os.path.join(path, fname) for fname in os.listdir(path)])
    else:
        assert os.path.exists(path)
        dem_list = [path]

    # Filter for needed strings in filenames, such as "_wgs84.tif"
    if fname_filter is not None:
        # Include only filenames that MATCH the match string.
        dem_list = [fn for fn in dem_list if (re.search(fname_filter, fn) is not None)]

    # Filter out unwanted filename strings.
    if fname_omit is not None:
        # Only include filenames that DO NOT MATCH the omission string.
        dem_list = [fn for fn in dem_list if (re.search(fname_omit, fn) is None)]

    # if use_icesat2_photon_database:
    # Generate a single photon database object and pass it repeatedly to all the objects.
    # This saves us a lot of re-reading the geodataframe repeatedly.
    # photon_db_obj = icesat2_photon_database.ICESat2_Database()
    photon_db_obj = ivert.icesat2_database_v2.IS2Database()

    files_to_export = []
    list_of_results_dfs = []
    list_of_empty_files = []

    # For each DEM, validate it.
    for i, dem_path in enumerate(dem_list):
        if verbose:
            print(
                "\n=======",
                os.path.split(dem_path)[1],
                "(" + str(i + 1),
                "of",
                str(len(dem_list)) + ")",
                "=======",
            )

        if output_dir is None:
            this_output_dir = os.path.split(dem_path)[0]
        elif os.path.isdir(output_dir):
            this_output_dir = output_dir
        else:
            # If it's a relative dir, append it to where the dems are.
            this_output_dir = os.path.join(os.path.dirname(dem_list[0]), output_dir)
            if not os.path.exists(this_output_dir):
                os.mkdir(this_output_dir)

        results_h5_file = os.path.join(
            this_output_dir,
            os.path.splitext(os.path.split(dem_path)[1])[0] + "_results.h5",
        )
        empty_fname = results_h5_file.replace("_results.h5", "_results_EMPTY.txt")

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
                dem_ndv=dem_ndv,
                interim_data_dir=this_output_dir,
                overwrite=overwrite,
                delete_datafiles=delete_datafiles,
                write_result_tifs=write_result_tifs,
                write_summary_stats=create_individual_results,
                include_photon_level_validation=include_photon_validation,
                plot_results=create_individual_results,
                outliers_sd_threshold=outliers_sd_threshold,
                mark_empty_results=True,
                measure_coverage=measure_coverage,
                min_confidence_level=min_confidence_level,
                min_bathy_confidence=min_bathy_confidence,
                export_error_formats=export_error_formats,
                exclude_zones=exclude_zones,
                verbose=verbose,
            )
        except MemoryError:
            if verbose:
                print(f"Skipping {os.path.basename(dem_path)} due to memory error.")
            continue

        except KeyboardInterrupt as e:
            raise e

        except Exception:
            if verbose:
                print(
                    f"Skipping {os.path.basename(dem_path)}: {traceback.format_exc()}",
                )
            continue

        files_to_export.extend(list(shared_ret_values.values()))

        if os.path.exists(results_h5_file):
            list_of_results_dfs.append(results_h5_file)

        elif os.path.exists(empty_fname):
            list_of_empty_files.append(empty_fname)

    # An extra newline is appreciated here just for readability's sake.
    if verbose:
        print()

    if len(list_of_results_dfs) == 0:
        if verbose:
            print("No results dataframes generated. Aborting.")
        return None

    # Generate the overall summary stats file.
    total_results_df = plot_validation_results.get_data_from_h5_or_list(
        list_of_results_dfs,
        orig_filenames=dem_list,
        include_filenames=True,
        verbose=verbose,
    )

    if write_summary_csv:
        write_summary_csv_file(
            total_results_df,
            list_of_empty_files,
            csv_name,
            verbose=verbose,
        )
        files_to_export.append(csv_name)

    # Output the statistics summary file.
    validate_dem.write_summary_stats_file(
        total_results_df,
        statsfile_name,
        verbose=verbose,
    )
    files_to_export.append(statsfile_name)

    # Output the validation results plot.
    plot_validation_results.plot_histograms_and_line(
        total_results_df,
        plot_file_name,
        place_name=place_name,
        verbose=verbose,
    )
    files_to_export.append(plot_file_name)

    if results_h5 is not None:
        total_results_df.to_hdf(results_h5, key="results", complib="zlib", complevel=3)
        if verbose:
            print(results_h5, "written.")
        files_to_export.append(results_h5)

    return files_to_export


@click.command(
    help="Tool for validating a list or directory of DEMs against ICESat-2 photon data.",
)
@click.argument("directory_or_files", type=str, nargs=-1, required=True)
@click.option(
    "--fname_filter",
    "-ff",
    type=str,
    default=r"\.tif\Z",
    help=r"A regex string to search for in all DEM file names, to use as a filter. Defaults to "
    "r'\\.tif\\Z', indicating .tif at the end of the file name. Helps elimiate files that "
    "shouldn't be considered.",
)
@click.option(
    "--fname_omit",
    "-fo",
    type=str,
    default=None,
    help="A regex string to search for and OMIT if it contains a match in the file name. Useful "
    "for avoiding derived datasets (such as converted DEMs) in the folder.",
)
@click.option(
    "--output_dir",
    "-od",
    type=str,
    default=None,
    help="Directory to output results. Default to the a sub-directory named 'icesat2' within the "
    "input directory.",
)
@click.option(
    "--input_vdatum",
    "-ivd",
    default="egm2008",
    help="The vertical datum of the input DEMs. Default: 'egm2008'",
)
@click.option(
    "--place_name",
    "-name",
    type=str,
    default=None,
    help="Readable name of the location being validated. Will be used in output summary plots and "
    "validation report.",
)
@click.option(
    "--overwrite",
    "-o",
    is_flag=True,
    default=False,
    help="Overwrite all files, including intermittent data files. Default: False "
    "(skips re-computing already-computed reseults.",
)
@click.option(
    "-cf",
    "--create_folders",
    type=yes_no.interpret_yes_no,
    default=True,
    help="Create folders specified in -output_dir and -data_dir, as well as the full path to "
    "-photon_h5, if they do not already exist. Otherwise will raise errors if directories "
    "don't already exist. Default: True.",
)
# @click.option("-mob", "--mask_osm_buildings",
#               type=yes_no.interpret_yes_no, default=True,
#               help="Whether to mask out OSM-derived building footprints in the coastline mask. "
#                    "Must be followed by 'True', 'False', 'Yes', 'No', or any abbreviation thereof "
#                    "(case-insensitive). (Default: True)")
#
# @click.option("-mbb", "--mask_bing_buildings",
#               type=yes_no.interpret_yes_no, default=True,
#               help="Whether to mask out Bing-derived building footprints in the coastline mask. "
#                    "Must be followed by 'True', 'False', 'Yes', 'No', or any abbreviation thereof "
#                    "(case-insensitive). (Default: True)")
#
# @click.option("-mwsf", "--mask_wsf_urban",
#               type=yes_no.interpret_yes_no, default=False,
#               help="Whether to mask out World-Settlement-Footprint heavy urban areas in the "
#                    "coastline mask. Typically used instead of building footprints for coarse DEMs "
#                    "with grid cells larger than typical buildings (~20-ish m). Must be followed by "
#                    "'True', 'False', 'Yes', 'No', or any abbreviation thereof (case-insensitive). "
#                    "(Default: False)")
#
# @click.option("-ml", "--mask_lakes",
#               type=yes_no.interpret_yes_no, default=True,
#               help="Whether to make out lakes, using Hydrolakes and the National Hydrologic Dataset. "
#                    "(Default: True)")
@click.option(
    "-ind",
    "--individual_results",
    type=yes_no.interpret_yes_no,
    default=True,
    help="By default, a summary plot and text file are generated for the dataset. If this is "
    "selected, they will be generated for each individual DEM as well. Files will be placed "
    "in the -output_dir directory. Default: True",
)
@click.option(
    "--include_photon_validation",
    "-ph",
    is_flag=True,
    default=False,
    help="Produce a photon database (stored in '*_photon_level_results.h5') with errors on a "
    "photon-level (not cell-level) scale. Useful for identifying bad ICESat-2 granules.",
)
@click.option(
    "--delete_datafiles",
    "-del",
    is_flag=True,
    default=False,
    help="By default, all data files generted in this process are kept. If this option is chosen, "
    "delete them.",
)
@click.option(
    "--outlier_sd_threshold",
    default="2.5",
    help="Number of standard-deviations away from the mean to omit outliers. Default 2.5. "
    "May choose 'None' if no filtering is requested.",
)
@click.option(
    "--measure_coverage",
    "-mc",
    is_flag=True,
    default=False,
    help="Measure the coverage %age of icesat-2 data in each of the output DEM cells.",
)
@click.option(
    "-wrt",
    "--write_result_tifs",
    type=yes_no.interpret_yes_no,
    default=True,
    help="Write output geotiff with the errors in cells that have ICESat-2 photons, "
    "NDVs elsewhere. Default: True",
)
@click.option(
    "-wsc",
    "--write_summary_csv",
    type=yes_no.interpret_yes_no,
    default=True,
    help="Write a CSV with summary results of each individual DEM.",
)
@click.option("--quiet", "-q", is_flag=True, default=False, help="Suppress output.")
def main(
    directory_or_files,
    fname_filter,
    fname_omit,
    output_dir,
    input_vdatum,
    place_name,
    overwrite,
    create_folders,
    individual_results,
    include_photon_validation,
    delete_datafiles,
    outlier_sd_threshold,
    measure_coverage,
    write_result_tifs,
    write_summary_csv,
    quiet,
):
    """Validate a list or directory of DEMs against ICESat-2 photon data.

    DIRECTORY_OR_FILES is a directory path, or a list of individual DEM tiles.
    """
    directory_or_files = list(directory_or_files)

    if output_dir is None:
        # Default to data dir or directory of first data file, in the 'icesat2' sub-directory.
        path = directory_or_files[0]
        if type(path) is str and os.path.isdir(path):
            output_dir = os.path.join(path, "icesat2")
        else:
            output_dir = os.path.join(os.path.split(path)[0], "icesat2")

        assert type(output_dir) is str

    if (type(fname_filter) in (list, tuple)) and (len(fname_filter) == 1):
        fname_filter = fname_filter[0]

    # Check for the existence of appropriate output & data directories.

    output_dir_abs = os.path.abspath(output_dir)
    if not os.path.exists(output_dir_abs):
        if create_folders:
            os.makedirs(output_dir_abs)
        else:
            raise FileNotFoundError(
                f"Output directory '{output_dir}' does not exist. "
                "Create directory or use the --create_folders flag upon execution.",
            )

    # NOTE: This code assumes that if we create the directory here, it will
    # not be erased before the code gets to putting files there later. Seems
    # like a generally safe assumption, and behavior is okay if another process
    # or the user manually deletes directories during execution, it will cause
    # the program to crash when it tries to write files there. That's a user error.

    # Set up multiprocessing. 'spawn' is the slowest but the most reliable. Otherwise, file handlers are fucking us up.
    mp.set_start_method("spawn")

    validate_list_of_dems(
        directory_or_files,
        fname_filter=fname_filter,
        fname_omit=fname_omit,
        output_dir=output_dir,
        input_vdatum=input_vdatum,
        overwrite=overwrite,
        place_name=place_name,
        create_individual_results=individual_results,
        delete_datafiles=delete_datafiles,
        include_photon_validation=include_photon_validation,
        write_result_tifs=write_result_tifs,
        # mask_osm_buildings=mask_osm_buildings,
        # mask_bing_buildings=mask_bing_buildings,
        # mask_wsf_urban=mask_wsf_urban,
        # mask_out_lakes=mask_lakes,
        # omit_bad_granules=True,
        measure_coverage=measure_coverage,
        write_summary_csv=write_summary_csv,
        outliers_sd_threshold=ast.literal_eval(outlier_sd_threshold),
        verbose=not quiet,
    )


if __name__ == "__main__":
    main()
