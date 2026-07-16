# -*- coding: utf-8 -*-

"""find_bad_icesat2_granules.py -- code for identifying and eliminating bad ICESat-2 granules from analyses."""

import os
import sys
import pandas
import numpy
import matplotlib.pyplot as plt
import re
import argparse
# import scipy.stats

####################################3
# Include the base /src/ directory of thie project, to add all the other modules.
# import import_parent_dir; import_parent_dir.import_src_dir_via_pythonpath()
####################################3
import validate_dem
import atl_granules
import utils.configfile
import utils.traverse_directory
import utils.parallel_funcs
import s3
# import datasets.etopo_source_dataset

my_config = utils.configfile.Config()

# def create_bad_granules_master_csv(bad_granule_list_fname = my_config._abspath(my_config.icesat2_bad_granules_list),
#                                     dataset_name = "CopernicusDEM",
#                                     overwrite=False,
#                                     verbose=True):
#     """Create a CSV file with a record of the bad granules we've identified from validating that dataset.

#     We assume that """


# def add_file_to_bad_granule_list(granule_fname,
#                                  bad_granule_list_fname = my_config._abspath(my_config.icesat2_bad_granules_list)):
#     """If we've found a bad granule in the list, add it to the list of bad granules.
#     In this case we're using the ATL03..._photons.h5 files."""

# def identify_bad_granules_in_single_validation(dem_fname,
#                                                verbose=True):
#     """Run a single-validation on a DEM, on a photon by photon basis.
#     Identify granules where the photon errors are consistently outside the ranges
#     of others by more than 2 s.d. (3 s.d.?) away from the mean range of the overall photons."""

# def get_summary_h5_fname_from_tilename(dem_fname):
#     """Given a DEM fname, return the file path of validation results file in the granule_validation_results directory."""
#     fname_short = os.path.split(dem_fname)
#     fname_base, ext = os.path.splitext(fname_short)
#     return os.path.join(my_config.icesat2_granule_validation_results_directory,
#                         fname_base + "_granule_results.h5")

# def plot_granule_histograms(granule_ids,
#                             granule_data_series,
#                             hist_fname = None,
#                             bad_granule_ids_to_label = None):
#     """Plot a stacked histogram showing the bad granules."""


def collect_granule_level_data(
    photon_results_df, lo_pct_cutoff=0.05, hi_pct_cutoff=99.95
):
    """Given a photon-level validation results file, return granule-level comparisons of data.
    Also compute the histogram if it's asked for, and plot it.
    """
    if type(photon_results_df) == str:
        df = pandas.read_hdf(photon_results_df, mode="r")
    elif type(photon_results_df) == pandas.DataFrame:
        df = photon_results_df
    else:
        raise TypeError(
            "Unknown type of 'photon_results_dataframe' in collect_granule_level_data():",
            type(photon_results_df),
        )

    # Get some cutoff boundaries in order to eliminate distant outliers from the histogram plots.
    low, hi = numpy.percentile(df.dem_minus_is2_m, (lo_pct_cutoff, hi_pct_cutoff))
    gids = numpy.unique(list(zip(df.granule_id1, df.granule_id2)), axis=0)
    piles = []
    for id1, id2 in gids:
        piles.append(
            df[
                (df.granule_id1 == id1)
                & (df.granule_id2 == id2)
                & (df.dem_minus_is2_m >= low)
                & (df.dem_minus_is2_m <= hi)
            ].dem_minus_is2_m
        )
    return gids, piles


def plot_stacked_histograms_with_bad_granule_marked(
    dem_fname, photon_db_fname, bad_gids_list, nbins=200, verbose=True
):

    photon_df = pandas.read_hdf(photon_db_fname)
    assert isinstance(photon_df, pandas.DataFrame)

    # granule_hist_plotname = get_photon_histogram_plotname_from_dem_fname(dem_fname, results_subdir=results_subdir)
    granule_hist_plotname = os.path.join(
        os.path.dirname(photon_db_fname),
        os.path.basename(dem_fname).replace(".tif", "_bad_granule_histogram.png"),
    )
    gids, piles = collect_granule_level_data(photon_df)

    # - 'gids' is a Nx2 list of granule-id pairs. All the other data corresponds with this list.
    # - 'piles' is a list of pandas.Series objects with the errors from each dataframe.
    # - 'h' is the stacked histogram data returned by plt.hist, consisting of:
    # - an N-tuple of all the histogram totals for each bin of the histogram.
    #     'N' is the number of different granule-id pairs. Each list is 'nbins' long.
    # - a array of the histogram box boundaries. This array is nbins+1 long.
    # - a list of N BarContainer objects, from matplotlib.pyplot.hist (probably not useful but we'll include in case it is).

    # Generate the axes and histogram.
    fig, ax = plt.subplots(dpi=600)
    gid_totals, bin_boundaries, _ = ax.hist(
        piles, bins=int(nbins), histtype="bar", stacked=True
    )

    # for i, (p, gt) in enumerate(zip(piles, gid_totals)):
    #     print("====================", i, "++++++++++++++++++")
    #     print(numpy.mean(p), numpy.std(p))
    #     print(numpy.sum(gt), "of", len(photon_df))
    #     print(numpy.argmax(gt), bin_boundaries[numpy.argmax(gt)])
    #     print(gt)

    # For each bad granule, put its name on the plot.
    for gname in bad_gids_list:
        gid1, gid2 = atl_granules.granule_id_to_intx2(gname)
        # gname = atl_granules.intx2_to_granule_id((gid1, gid2))

        # Find that bad granule in the list of all the granules in this file.
        for i, (tgid1, tgid2) in enumerate(gids):
            if (tgid1 == gid1) and (tgid2 == gid2):
                break

        gid_data = piles[i]
        gid_mean = numpy.mean(gid_data)
        gid_std = numpy.std(gid_data)
        # Since these are "stacked" histograms, I gotta subtract the histogram before
        hist_totals = gid_totals[i] - (0 if i == 0 else gid_totals[i - 1])
        max_j = numpy.argmax(hist_totals)
        max_y = hist_totals[max_j]
        # max_x = numpy.mean(bin_boundaries[max_j:max_j+2])

        # Plot the GID name hovering over the histogram.
        text_y = max_y * 1.15
        x_buffer = (bin_boundaries[-1] - bin_boundaries[0]) * 0.02
        if max_j < (len(hist_totals) / 2):
            halign = "left"
            text_x = bin_boundaries[0] + x_buffer
        else:
            halign = "right"
            text_x = bin_boundaries[-1] - x_buffer

        if os.path.splitext(gname)[1] == "":
            gname = gname + ".h5"
        # Put the text of the granule name on there.
        ax.text(text_x, text_y, gname, ha=halign, va="bottom", size="small")

        # Add mean & 2-stdev bars
        # Center vertical line
        ax.plot([gid_mean, gid_mean], [0, max_y * 0.9], lw=1, color="black")
        # Horizontal line
        ax.plot(
            [gid_mean - 2 * gid_std, gid_mean + 2 * gid_std],
            [max_y * 0.9 / 2, max_y * 0.9 / 2],
            lw=0.5,
            linestyle="--",
            color="black",
        )
        # Left vertical line
        ax.plot(
            [gid_mean - 2 * gid_std, gid_mean - 2 * gid_std],
            [max_y * 0.20, max_y * 0.70],
            lw=0.7,
            color="black",
        )
        # Right vertical line
        ax.plot(
            [gid_mean + 2 * gid_std, gid_mean + 2 * gid_std],
            [max_y * 0.20, max_y * 0.70],
            lw=0.7,
            color="black",
        )

    ax.set_xlabel("Error (m)")
    ax.set_ylabel("Count")
    fig.suptitle(os.path.split(dem_fname)[1])

    fig.tight_layout()
    fig.savefig(granule_hist_plotname)
    if verbose:
        print(
            os.path.join(*(os.path.normpath(granule_hist_plotname).split(os.sep)[-3:])),
            "written.",
        )

    return


def find_bad_granules_in_a_dataset(
    results_dirname,
    photon_results_regex=r"_results_photon_level_results\.h5\Z",
    make_histogram_plots_if_bad=True,
    recurse=True,
    nprocs=20,
    verbose=True,
):
    """Loop through all the validation results of a dataset that has been validated.
    Look for all photon-level validation results produced previously and tag all
    the bad granules found with the '_BAD_GRANULES.csv' file.

    Return a dataframe of all the bad granules found, along with the number of DEMs in which that bad granule was found.
    Save the dataframe to "csv_list_output."""
    # if type(dataset_name_or_object) == str:
    #     dset = datasets.etopo_source_dataset.get_source_dataset_object(dataset_name_or_object)
    #
    # elif isinstance(dataset_name_or_object, datasets.etopo_source_dataset.ETOPO_source_dataset) \
    #     or issubclass(dataset_name_or_object, datasets.etopo_source_dataset.ETOPO_source_dataset):
    #     dset = dataset_name_or_object

    # else:
    #     raise TypeError("Unhandled object type given for parameter 'dataset_name_or_object':", type(dataset_name_or_object))

    # list_of_datafiles = dset.retrieve_all_datafiles_list()
    if recurse:
        list_of_photon_results_datasets = utils.traverse_directory.list_files(
            results_dirname, regex_match=photon_results_regex
        )
    else:
        list_of_photon_results_datasets = [
            os.path.join(results_dirname, fn)
            for fn in os.listdir(results_dirname)
            if re.search(photon_results_regex, fn) is not None
        ]

    # Find the DEM name (.tif) associated with each photon_database fname in the same directory.
    fbases = [
        os.path.basename(fn[: re.search(photon_results_regex, fn).span()[0]]) + ".tif"
        for fn in list_of_photon_results_datasets
    ]
    tif_list = utils.traverse_directory.list_files(
        results_dirname, regex_match=r"\.tif\Z", depth=-1 if recurse else 1
    )
    list_of_dems = [None] * len(fbases)
    for i, fb in enumerate(fbases):
        for tif in tif_list:
            if tif.find(fb) > -1:
                list_of_dems[i] = tif
                continue
    # Every one of these results databases should have a DEM associated with it.
    assert numpy.all([(dem is not None) for dem in list_of_dems])

    # Get absolute paths for each of these files.
    list_of_photon_results_datasets = [
        os.path.abspath(fn) for fn in list_of_photon_results_datasets
    ]
    list_of_dems = [os.path.abspath(fn) for fn in list_of_dems]

    # list_of_dems = [os.path.basename(fn[:re.search(photon_results_regex, fn).span()[0]] + "_results.h5") for fn in list_of_photon_results_datasets]

    # list_of_filenames_containing_bad_granules = []
    if verbose:
        print(
            "Starting loop of {0} photon results to validate.".format(len(list_of_dems))
        )
        sys.stdout.flush()

    #
    # for i,(dem_fname,photon_df_name) in enumerate(zip(list_of_dems, list_of_photon_results_datasets)):
    #     if verbose:
    #         print("{0}/{1}".format(i+1, len(list_of_dems)), photon_df_name, dem_fname)
    # foobar

    # # If we've already created a "BAD_GRANULES.csv" file from this DEM, skip it.
    # if os.path.exists(bad_granules_csv_name):
    #     continue

    # Step through, find the granules that are separated from one another.
    utils.parallel_funcs.process_parallel(
        find_granules_that_are_separate_from_others_in_a_dem,
        args_lists=[
            [dem, phf]
            for dem, phf in zip(list_of_dems, list_of_photon_results_datasets)
        ],
        kwargs_list={
            "write_bad_granules_csv_if_needed": True,
            "plot_distributions_if_bad_granules": True,
            "remove_photon_db_if_no_bad_granules": False,
            "verbose": False,
        },
        max_nprocs=nprocs,
        verbose=verbose,
    )

    # bad_granule_ids, photon_df = \
    #     find_granules_that_are_separate_from_others_in_a_dem(dem_fname,
    #                                                          also_return_photon_df = True,
    #                                                          skip_if_no_photon_results_file = True,
    #                                                          write_bad_granules_csv_if_needed = True,
    #                                                          verbose = verbose)

    # TODO: Change name convention here.
    # bad_granules_csv_names = [get_bad_granule_csv_name_from_dem_fname(fn, results_subdir=None) for fn in list_of_dems]
    bad_granules_csv_names = [
        os.path.join(
            os.path.dirname(pdb),
            os.path.basename(dem).replace(".tif", "_BAD_GRANULES.csv"),
        )
        for (dem, pdb) in zip(list_of_dems, list_of_photon_results_datasets)
    ]
    list_of_bad_granule_ids = []

    for i, (dem_fname, photon_db_fname, bg_csv_name) in enumerate(
        zip(list_of_dems, list_of_photon_results_datasets, bad_granules_csv_names)
    ):
        if not os.path.exists(bg_csv_name):
            continue

        bad_granule_ids = pandas.read_csv(bg_csv_name)["granule_name"].tolist()

        if len(bad_granule_ids) > 0:
            # if verbose:
            #     print(len(bad_granule_ids), "bad granules found.")
            assert os.path.exists(bg_csv_name)
            list_of_bad_granule_ids.extend(bad_granule_ids)

            # print(bad_granule_ids)

            if make_histogram_plots_if_bad:
                plot_stacked_histograms_with_bad_granule_marked(
                    dem_fname, photon_db_fname, bad_granule_ids, verbose=verbose
                )
        # elif verbose:
        #     print(" done.")

    # Convert to a set to remove duplicates, than back to a (sorted) list.
    return sorted(list(set(list_of_bad_granule_ids)))


# def get_photon_validation_fname_from_dem_fname(dem_fname,
#                                                results_subdir='icesat2_results',
#                                                include_results_df_name = False):
#     base_path, fname = os.path.split(dem_fname)
#     # If the results directory doesn't exist yet, create it.
#     if results_subdir is not None:
#         results_dir = os.path.join(base_path, results_subdir)
#     else:
#         results_dir = base_path
#
#     fbase, ext = os.path.splitext(fname)
#     results_database_name = os.path.join(results_dir, fbase + "_results.h5")
#
#     base, ext = os.path.splitext(results_database_name)
#     photon_results_database_name = base + "_photon_level_results.h5"
#     if include_results_df_name:
#         return photon_results_database_name, results_database_name
#     else:
#         return photon_results_database_name
#
# def get_bad_granule_csv_name_from_dem_fname(dem_fname,
#                                             results_subdir='icesat2_results'):
#     base_path, fname = os.path.split(dem_fname)
#     # If the results directory doesn't exist yet, create it.
#     if results_subdir is not None:
#         results_dir = os.path.join(base_path, results_subdir)
#     else:
#         results_dir = base_path
#
#     fbase, ext = os.path.splitext(fname)
#     bad_granule_csv_name = os.path.join(results_dir, fbase + "_BAD_GRANULES.csv")
#     return bad_granule_csv_name
#
# def get_granule_stats_h5_name_from_dem_fname(dem_fname,
#                                              results_dir = 'icesat2_results'):
#     base_path, fname = os.path.split(dem_fname)
#     # If the results directory doesn't exist yet, create it.
#     if results_subdir is not None:
#         results_dir = os.path.join(base_path, results_subdir)
#     else:
#         results_dir = base_path
#
#     fbase, ext = os.path.splitext(fname)
#     bad_granule_csv_name = os.path.join(results_dir, fbase + "_granule_stats.h5")
#     return bad_granule_csv_name
#
# def get_photon_histogram_plotname_from_dem_fname(dem_fname,
#                                                  results_subdir = "icesat2_results"):
#     base_path, fname = os.path.split(dem_fname)
#     # If the results directory doesn't exist yet, create it.
#     if results_subdir is not None:
#         results_dir = os.path.join(base_path, results_subdir)
#     else:
#         results_dir = base_path
#
#     fbase, ext = os.path.splitext(fname)
#     bad_granule_png_name = os.path.join(results_dir, fbase + "_bad_granule_histogram.png")
#     return bad_granule_png_name


def generate_photon_database_validation(
    dem_fname,
    photon_db_fname,
    dem_vdatum,
    vdatum_out="EGM2008",
    overwrite=False,  # Probably need to add variables for cutoffs here.
    verbose=True,
):
    """Run a validation on the fname and tally up granule-level photon-by-photon
    validation results for each granule within the DEM bounding box.

    Return a dataframe with the results for that tile, delineated by granules within that tile.
    Also, write the dataframe out to an HDF5 to the results dataframe.
    """
    # 1. Get the photon validation dataframe for that tile *on a photon basis*. *Not* aggregated by grid-cell.
    results_subdir = "icesat2_results"
    base_path, fname = os.path.split(dem_fname)
    # If the results directory doesn't exist yet, create it.
    results_dir = os.path.join(base_path, results_subdir)
    if not os.path.exists(results_dir):
        os.mkdir(results_dir)

    # photon_results_database_name, results_database_name = get_photon_validation_fname_from_dem_fname(dem_fname,
    #                                                                                                  resluts_subdir=results_subdir,
    #                                                                                                  include_results_df_name=True)
    results_database_name = os.path.join(
        os.path.dirname(photon_db_fname),
        os.path.basename(dem_fname).replace(".tif", "_results.h5"),
    )
    # Check to make sure it's not already in there (skip if it is, unless we've specified to overwrite it.)
    if os.path.exists(photon_db_fname):
        if overwrite:
            if verbose:
                print("Removing old", os.path.split(photon_db_fname)[1] + ".")
            os.remove(photon_db_fname)
        else:
            if verbose:
                print(os.path.split(photon_db_fname)[1], "already exists.")
            return

    # Check to make sure it's not already in there (skip if it is, unless we've specified to overwrite it.)
    if os.path.exists(results_database_name):
        if overwrite:
            if verbose:
                print("Removing old", os.path.split(results_database_name)[1] + ".")
            os.remove(results_database_name)
        else:
            if verbose:
                print(os.path.split(results_database_name)[1], "already exists.")
            return

    # else: # not os.path.exists(photon_results_database_name)
    # If we don't have a photon-level validations file, make sure we generate one
    # Key point: ensure "include_photon_level_validation = True".
    validate_dem.validate_dem_parallel(
        dem_fname,
        photon_dataframe_name=photon_db_fname,
        use_icesat2_photon_database=True,
        icesat2_photon_database_obj=None,
        dem_vertical_datum=dem_vdatum,
        output_vertical_datum=vdatum_out,
        results_dataframe_file=results_database_name,
        interim_data_dir=results_dir,
        include_photon_level_validation=True,
        quiet=not verbose,
    )

    return photon_db_fname


def find_granules_that_are_separate_from_others_in_a_dem(
    dem_fname,
    photon_db_fname,
    save_granule_stats_df=False,
    remove_granules_with_less_than_N_photons=20,
    write_bad_granules_csv_if_needed=True,
    plot_distributions_if_bad_granules=True,
    remove_photon_db_if_no_bad_granules=False,
    verbose=True,
):
    """For a given fname, perform a validation (if not done already) and compute the photon-level
    validation stats for the DEM. Then, compute a granule-by-granule 2-sided KS stat to
    determine whether these granules come from the same "population". Save the results to a new dataframe in the same
    directory as the photon_db_fname."""

    # 1. Loop through all the unique granules and organize the photons by those.
    # Read the photon_level validations dataframe
    photon_df = pandas.read_hdf(photon_db_fname, mode="r")
    # A Nx2 array of uniqpe granule-id integer pairs.
    # unique_granule_ids = numpy.unique(list(zip(photon_df.granule_id1, photon_df.granule_id2)))

    granule_ids, granule_data = collect_granule_level_data(photon_df)

    # Remove any granules that have less than the requisite number of photons in them in this database.
    granule_idxs_to_remove = []
    for i, g_data in enumerate(granule_data):
        if len(g_data) < remove_granules_with_less_than_N_photons:
            granule_idxs_to_remove.append(i)
    # Any of the granules that were identified in the last loop, remove them.
    # Go through the list backward to not screw up the indices if there is more than one.
    granule_idxs_to_remove.reverse()
    for i in granule_idxs_to_remove:
        granule_ids = numpy.concatenate(
            (granule_ids[:i, :], granule_ids[i + 1 :, :]), axis=0
        )
        granule_data = granule_data[:i] + granule_data[i + 1 :]

    granule_stats_results_tuples = []

    for i, ((gid1, gid2), g_data) in enumerate(zip(granule_ids, granule_data)):
        # In the next loop, only do the granules that come after the first granule ([i+1:]),
        # so that we skip the 2x redundancy of comparing (g1, g2) and later (g2, g1)
        # in reverse order, which will give the exact same answer.
        for (gid1_other, gid2_other), g_data_other in list(
            zip(granule_ids, granule_data)
        )[i + 1 :]:
            # Skip if they are the same granule. No need to compare with oneself.
            # This logic shouldn't be needed if the above loopling logic works as intended.
            # But it's harmless to keep it here and would help if it comes to it,
            # So just skip over any granule self-comparisons that happen by accident.
            if gid1 == gid1_other and gid2 == gid2_other:
                continue

            # Apply a 2-sided, 2-sample Kolmogorov-Smirnov test to see if the two
            # sets of DEM errors from different granules come from the same approximate
            # distribution. Most of them are pretty close, perhaps slightly different shapes,
            # but some of they are *way* off, with highly-different means and distributions.
            # Find those. Test on some known samples first.
            # See https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ks_2samp.html for documentation

            # Note: The KS stat isn't working great, because these different granules paths *are* different
            # statistical populations, so it thinks they all are different. Gives high KS scores
            # and low p-values for nearly literally every pair. It's not wrong, but it's too sensitive for
            # our needs. Instead, we just want to know if the 2-s.d. range of one granule is
            # outside the 2-s.d. range of *all* the other granules (perhaps except one?). If so, mark it
            # as a 'bad' granule. We will later check to see if this granule appears as 'bad'
            # in more than one DEM validation, in which case we can probably infer it doesn't have
            # good data.
            # ks_stat, p_value = scipy.stats.ks_2samp(g_data, g_data_other, alternative="two-sided", mode='auto')
            g1_mean = g_data.mean()
            g1_std = g_data.std()
            g1_size = len(g_data)
            g2_mean = g_data_other.mean()
            g2_std = g_data_other.std()
            g2_size = len(g_data_other)

            granule_stats_results_tuples.append(
                (
                    gid1,
                    gid2,
                    g1_size,
                    g1_mean,
                    g1_std,
                    gid1_other,
                    gid2_other,
                    g2_size,
                    g2_mean,
                    g2_std,
                )
                # ks_stat,
                # p_value )
            )
    # Create a little dataframe from this.
    grdf = pandas.DataFrame(
        data={
            "g1_id1": [n[0] for n in granule_stats_results_tuples],
            "g1_id2": [n[1] for n in granule_stats_results_tuples],
            "g1_size": [n[2] for n in granule_stats_results_tuples],
            "g1_mean": [n[3] for n in granule_stats_results_tuples],
            "g1_std": [n[4] for n in granule_stats_results_tuples],
            "g2_id1": [n[5] for n in granule_stats_results_tuples],
            "g2_id2": [n[6] for n in granule_stats_results_tuples],
            "g2_size": [n[7] for n in granule_stats_results_tuples],
            "g2_mean": [n[8] for n in granule_stats_results_tuples],
            "g2_std": [n[9] for n in granule_stats_results_tuples],
        }
    )
    # "ks_stat" : [n[10] for n in granule_ks_results_tuples],
    # "p_value" : [n[11] for n in granule_ks_results_tuples],

    # A simple test to see if the mean of each granule is outside +/- 2 SD of the mean of the other, for both granules.
    # ks_test_df["is_outside"] = ~(((ks_test_df.g1_mean - (2 * ks_test_df.g1_std)) < ks_test_df.g2_mean) & \
    #                              ((ks_test_df.g1_mean + (2 * ks_test_df.g1_std)) > ks_test_df.g2_mean) & \
    #                              ((ks_test_df.g2_mean - (2 * ks_test_df.g2_std)) < ks_test_df.g1_mean) & \
    #                              ((ks_test_df.g2_mean + (2 * ks_test_df.g2_std)) > ks_test_df.g1_mean))

    # Another simple test to see if the mean +/- 2-SD envelopes of both granules do not at all overlap.
    # (i.e if the "top" of one 2-SD envelop is less than the "bottom" of the other, in either direction.)
    grdf["is_outside"] = (
        (grdf.g1_mean - (2 * grdf.g1_std)) > (grdf.g2_mean + (2 * grdf.g2_std))
    ) | ((grdf.g2_mean - (2 * grdf.g2_std)) > (grdf.g1_mean + (2 * grdf.g1_std)))

    # Comment this out later.
    # print(grdf)
    if save_granule_stats_df:
        granule_stats_fname = os.path.join(
            os.path.dirname(photon_db_fname),
            os.path.basename(dem_fname).replace(".tif", "_granule_stats.h5"),
        )
        grdf.to_hdf(granule_stats_fname, "icesat2", complevel=3, complib="zlib")
        if verbose:
            print(granule_stats_fname, "written.")

    # Now, identify which granules have been identified as "outside" the 2-s.d. envelopes of literally
    # all (but an allowance of 1) other granules in this dataset.
    LIST_OF_OUTSIDE_GRANULES_IDS = []
    LIST_OF_PHOTON_COUNTS = []
    for gid1, gid2 in granule_ids:
        # Get all records that have this granule.
        grdf_this_granule = grdf[
            ((grdf["g1_id1"] == gid1) & (grdf["g1_id2"] == gid2))
            | ((grdf["g2_id1"] == gid1) & (grdf["g2_id2"] == gid2))
        ]
        # There should be 1 less record (all other granules) than the length of the whole list of granules.
        # Just check this logic here.
        assert (len(grdf_this_granule) + 1) == granule_ids.shape[0]

        # If the number of granules this granule is "ouside of" is greater than or
        # equal to 1 less than all the other granules in this dataset, and is
        # more than at least 2 other granules, then add it to the list of outside granule IDs.
        if 3 <= grdf_this_granule["is_outside"].sum() >= (len(grdf_this_granule) - 1):
            LIST_OF_OUTSIDE_GRANULES_IDS.append((gid1, gid2))
            photon_count = (
                (photon_df.granule_id1 == gid1) & (photon_df.granule_id2 == gid2)
            ).sum()
            LIST_OF_PHOTON_COUNTS.append(photon_count)

    if len(LIST_OF_OUTSIDE_GRANULES_IDS) > 0:
        granule_names = [
            atl_granules.intx2_to_granule_id(g) + ".h5"
            for g in LIST_OF_OUTSIDE_GRANULES_IDS
        ]

        if write_bad_granules_csv_if_needed:
            # csv_fname = get_bad_granule_csv_name_from_dem_fname(dem_fname, results_subdir=results_subdir)
            csv_fname = os.path.join(
                os.path.dirname(photon_db_fname),
                os.path.basename(dem_fname).replace(".tif", "_BAD_GRANULES.csv"),
            )
            outside_granule_df = pandas.DataFrame(
                data={
                    "granule_name": granule_names,
                    "gid1": [g[0] for g in LIST_OF_OUTSIDE_GRANULES_IDS],
                    "gid2": [g[1] for g in LIST_OF_OUTSIDE_GRANULES_IDS],
                    "photon_count": LIST_OF_PHOTON_COUNTS,
                },
            )
            outside_granule_df.to_csv(csv_fname, index=False)
            if verbose:
                print(
                    os.path.join(*(os.path.normpath(csv_fname).split(os.sep)[-3:])),
                    "written with {0} entries.".format(
                        len(LIST_OF_OUTSIDE_GRANULES_IDS)
                    ),
                )

        if plot_distributions_if_bad_granules:
            # Plot the data here.
            plot_stacked_histograms_with_bad_granule_marked(
                dem_fname, photon_db_fname, granule_names, verbose=verbose
            )

    if remove_photon_db_if_no_bad_granules and len(LIST_OF_OUTSIDE_GRANULES_IDS) == 0:
        os.remove(photon_db_fname)
        if verbose:
            print(os.path.basename(photon_db_fname), "removed.")

    return LIST_OF_OUTSIDE_GRANULES_IDS


def symlink_bad_granule_plots_to_histogram_dir(plotdir, histdir):
    """For all tiles that contain a "BAD_GRANULES.csv" file in the histdir, copy a hotlink to the "results_lot.png" file
    into the histogram directory.

    Just use a symlink to do this without copying/moving any actual files.

    Often, after running the 'bad granule-finder', I put all the "histogram plots" and
    associated csv files into a sub-directory to easily peruse them. It's handy to look at the results_plot's as well,
    to see if it's really a bad double-surface or just a random error. This helps do that."""
    tag_regex = r"[SN]\d{2}[EW]\d{3}"
    bad_granule_ids = [
        re.search(tag_regex, fn).group()
        for fn in os.listdir(histdir)
        if (fn.find("_BAD_GRANULES.csv") > 0)
    ]

    print(len(bad_granule_ids))

    plotfiles = [
        os.path.join(plotdir, fn)
        for fn in os.listdir(plotdir)
        if (fn.find("_results_plot.png") > 0)
        and (re.search(tag_regex, fn).group() in bad_granule_ids)
    ]

    for pfile in plotfiles:
        os.symlink(pfile, os.path.join(histdir, os.path.basename(pfile)))

    print(len(plotfiles), "plot files symlink'ed.")

    return


def accumulate_bad_granule_dfs(
    dirname, bad_granule_regex=r"_BAD_GRANULES\.csv\Z", verbose=True
):
    """Run through a whole dataset, find all the "_BAD_GRANULES.csv" filenames, accumulate them all into a collective dataframe.
    NOTE: Many may have repeat granule names from their respective DEMs. This is okay.
    Just return a dataframe that includes all the "_BAD_GRANULES.csv" entries, along with the filename they came from.
    Return this dataframe.
    """
    # if isinstance(dataset_name_or_obj, datasets.etopo_source_dataset.ETOPO_source_dataset):
    #     dset_obj = dataset_name_or_obj
    # else:
    #     dset_obj = datasets.etopo_source_dataset.get_source_dataset_object(dataset_name_or_obj)

    bad_granule_file_list = utils.traverse_directory.list_files(
        dirname, regex_match=bad_granule_regex, include_base_directory=True
    )
    # Make sure we're only dealing in absolute paths here.
    bad_granule_file_list = [os.path.abspath(p) for p in bad_granule_file_list]

    if len(bad_granule_file_list) > 0:
        bad_granule_dfs = [
            pandas.read_csv(fn, index_col=False) for fn in bad_granule_file_list
        ]

        # Add a column for csv_filename for where the file came from.
        for fn, df in zip(bad_granule_file_list, bad_granule_dfs):
            df["csv_filename"] = fn

        # Concatenate them into one dataframe
        return pandas.concat(bad_granule_dfs, ignore_index=True)
    else:
        return None


def create_master_list_of_bad_granules(
    dirname,
    master_bad_granule_csv=my_config._abspath(my_config.icesat2_bad_granules_csv),
    append=True,
    verbose=True,
):
    """Go through a dataset that has been validated with "photon-level results", and that has already had
    find_bad_granules_in_a_dataset() called on it.

    Pick out all the _BAD_GRANULES.csv files from it, add their records to the master list if they are not already in there.
    """
    new_bad_granules_df = accumulate_bad_granule_dfs(dirname, verbose=verbose)
    if new_bad_granules_df is None or len(new_bad_granules_df) == 0:
        if verbose:
            print("No bad granules found in dataset '{0}'".format(dirname))

        if append and os.path.exists(master_bad_granule_csv):
            existing_bad_granules_df = pandas.read_csv(
                master_bad_granule_csv, index_col=False
            )
            if verbose:
                print("Read existing", os.path.split(master_bad_granule_csv)[1])

            return existing_bad_granules_df
        else:
            return new_bad_granules_df

    if append:
        if os.path.exists(master_bad_granule_csv):
            existing_bad_granules_df = pandas.read_csv(
                master_bad_granule_csv, index_col=False
            )
            if verbose:
                print("Read existing", os.path.split(master_bad_granule_csv)[1])
        else:
            existing_bad_granules_df = None

        # Check whether the new bad granule records already exist (or not) in the existing db.
        # Create an n-length vector so later we can just filter out records that are actually new.
        new_records_mask = numpy.zeros((len(new_bad_granules_df),), dtype=bool)
        for idx, row in new_bad_granules_df.iterrows():
            # Mark "true" for any granule records that do not already exist in the dataframe from that DEM validation.
            if (existing_bad_granules_df.csv_filename == row.csv_filename).sum() == 0:
                new_records_mask[idx] = True

        if numpy.count_nonzero(new_records_mask) > 0:
            bad_granules_df = pandas.concat(
                [existing_bad_granules_df, new_bad_granules_df[new_records_mask]],
                axis=0,
                ignore_index=True,
            )
            print(
                "Added",
                numpy.count_nonzero(new_records_mask),
                "new granule records to",
                os.path.split(master_bad_granule_csv)[1],
            )
            bad_granules_df.to_csv(master_bad_granule_csv, index=False)
            print(
                os.path.split(master_bad_granule_csv)[1],
                "written with",
                len(bad_granules_df),
                "records.",
            )

            return bad_granules_df
        else:
            return existing_bad_granules_df

    else:
        new_bad_granules_df.to_csv(master_bad_granule_csv, index=False)
        if verbose:
            print(os.path.split(master_bad_granule_csv)[1], "written.")

        return new_bad_granules_df


def get_list_of_granules_to_reject(
    bad_granule_csv=my_config._abspath(my_config.icesat2_bad_granules_csv),
    get_from_s3_if_not_present=True,
    refind_bad_granules=False,
    regenerate_bad_granule_csv=False,
    append_if_regenerating_bad_granule_csv=True,
    dirname_if_regenerating="CUDEM_CONUS",  # TODO: Change this to Copernicus when ready to do the whole world.
    files_identified_threshold=2,
    min_photons_threshold=1000,
    return_as_gid_numbers=False,
    verbose=True,
):
    """Reading the 'bad_granules_list.csv' file, return a list of granules that
    have either been identified in 'files_identified_threshold' or more separate DEM
    validations as "bad", or that have a 'min_photons_threshold' minimum number of photons
    in one or more files, and have been identified as 'bad'.

    If return_as_gid_numbers, return a list of 2-tuple (gid1,gid2) granule identifiers.
    Otherwise, return a list of ATL03...h5 granule names."""
    if refind_bad_granules:
        find_bad_granules_in_a_dataset(dirname_if_regenerating)

    if regenerate_bad_granule_csv:
        create_master_list_of_bad_granules(
            dirname_if_regenerating,
            master_bad_granule_csv=bad_granule_csv,
            append=append_if_regenerating_bad_granule_csv,
            verbose=verbose,
        )

    if (
        not os.path.exists(bad_granule_csv)
        and get_from_s3_if_not_present
        and my_config.is_aws
    ):
        # If the bad_granule_list.csv does not exist locally, and we're in the cloud, and we've set
        # get_from_s3_if_not_present, download it.
        s3m = s3.S3Manager()
        s3m.download(
            my_config.s3_bad_granules_csv_key,
            bad_granule_csv,
            bucket_type="database",
            fail_quietly=not verbose,
        )

    if not os.path.exists(bad_granule_csv):
        if verbose:
            print(
                "Error: No bad_granule_list.csv file found. Not filtering out bad granules."
            )
        return []

    # Read the 'bad_granule_list.csv' file
    bad_granule_df = pandas.read_csv(bad_granule_csv, index_col=False)
    unique_granule_names = bad_granule_df["granule_name"].unique()

    granules_to_exclude = []
    for uniq_gr in unique_granule_names:
        subset_df = bad_granule_df[bad_granule_df["granule_name"] == uniq_gr]
        num_files_identified = len(subset_df)
        num_photons = subset_df["photon_count"].sum()

        if (num_files_identified >= files_identified_threshold) and (
            num_photons >= min_photons_threshold
        ):
            granules_to_exclude.append(uniq_gr)

    if return_as_gid_numbers:
        return [atl_granules.granule_id_to_intx2(gid) for gid in granules_to_exclude]
    else:
        return granules_to_exclude


def delete_results_with_bad_granules(
    dirname,
    dem_regex=r"v[123]\.tif\Z",
    photon_db_regex=r"_results_photon_level_results\.h5\Z",
    verbose=True,
):
    """Run through a results directory and elimiate all results that contain bad granules.

    It will be assumed that the "photon-level results" will have been generated, which
    simplifies finding results that contain photons from bad granules.

    Will not eliminate vdatum-converted DEMs or coastline masks, as those have
    nothing to do with bad granules. Those are kept in place.

    If "place_name" is given, if some bad granules were found, also eliminate
    the final cumulative results files so that those can be re-generated again as well.
    When done, re-run the "validate_dem_collection.py" script, this time including omission
    of bad-granule results, and the results will be regenerated.

    After running this, re-run the analysis over these areas to re-generate
    the results with the 'omit_bad_granules' flag attached.

    Return a list of the files deleted.
    """
    # if isinstance(dataset_name_or_obj, datasets.etopo_source_dataset.ETOPO_source_dataset):
    #     dset_obj = dataset_name_or_obj
    # else:
    #     dset_obj = datasets.etopo_source_dataset.get_source_dataset_object(dataset_name_or_obj)

    # List of all the DEMs
    dem_filenames_all = utils.traverse_directory.list_files(
        dirname, regex_match=dem_regex, include_base_directory=True
    )

    photon_df_filenames = utils.traverse_directory.list_files(
        dirname, regex_match=photon_db_regex, include_base_directory=True
    )

    # Get the base name of the dem .tif file that should be associated with each photon database filename.
    fbases = [
        os.path.basename(fn[: re.search(photon_db_regex, fn).span()[0]]) + ".tif"
        for fn in photon_df_filenames
    ]

    # Get the associated DEM filename for each photon_database filename.
    list_of_dems = [None] * len(photon_df_filenames)
    for i, fb in enumerate(fbases):
        for tif in dem_filenames_all:
            if tif.find(fb) > -1:
                list_of_dems[i] = tif
                continue
    # Every one of these results databases should have a DEM associated with it.
    assert numpy.all([(dem is not None) for dem in list_of_dems])

    # if recurse:
    #     dem_filenames = utils.traverse_directory.list_files(dirname, regex_match=dem_regex)
    # else:
    #     dem_filenames = [os.path.join(dirname, fn) for fn in os.listdir(dirname) if (re.search(dem_regex, fn) != None)]
    # # List of the bad granules, by GID integers.
    bad_gid_list = get_list_of_granules_to_reject(return_as_gid_numbers=True)

    # Get the directory where all the results are hidden.
    # if results_subdir != None:
    #     results_dir = os.path.join(dirname, results_subdir)
    # else:
    #     results_dir = dirname

    files_removed = []
    dirs_to_remove_summary_files = []

    for dem_fname, photon_db_fname in zip(list_of_dems, photon_df_filenames):
        # photon_results_df_regex = os.path.basename(dem_fname)[:os.path.basename(dem_fname).find(".tif")] + r"_results_photon_level_results\.h5\Z"
        # photon_results_df_list = utils.traverse_directory.list_files(os.path.dirname(os.path.dirname(dem_fname)), photon_results_df_regex)

        # if not os.path.exists(photon_results_df_fname):
        # if len(photon_results_df_list) == 0:
        #     if verbose:
        #             # print(os.path.split(photon_results_df_fname)[1], "not found. Moving on.")
        #             print("{0} not found in {1}. Moving on.".format(photon_results_df_regex, os.path.dirname(os.path.dirname(dem_fname))))
        #             continue
        # else:
        #     assert len(photon_results_df_list) == 1
        #     photon_results_df_fname = photon_results_df_list[0]

        photon_df = pandas.read_hdf(photon_db_fname)
        results_base = os.path.join(
            os.path.dirname(photon_db_fname),
            os.path.basename(dem_fname).replace(".tif", ""),
        )

        for gid1, gid2 in bad_gid_list:
            # If no photons in this dataset come from the bad granule, just move along to the next one.
            bad_g_mask = (photon_df["granule_id1"] == gid1) & (
                photon_df["granule_id2"] == gid2
            )
            if numpy.count_nonzero(bad_g_mask) == 0:
                continue

            # If we found any photons from a bad granule in this DEM validation,
            # eliminate all the results files from it.
            results_suffixes = [
                "_results.h5",
                "_results_ICESat2_error_map.tif",
                "_results_photon_level_results.h5",
                "_results_plot.png",
                "_results_summary_stats.txt",
            ]
            for suffix in results_suffixes:
                r_fname = results_base + suffix
                if os.path.exists(r_fname):
                    # Add it to our list of deleted files
                    files_removed.append(r_fname)
                    # Delete the file
                    os.remove(r_fname)
                    # Say that we deleted the file.
                    if verbose:
                        print(os.path.split(r_fname)[1], "deleted.")

            dirs_to_remove_summary_files.append(os.path.dirname(photon_db_fname))

            # After deleting the files, we don't need to keep looking for bad granule data in that file.
            break

    # If we've removed some files and the cumulative summary dataset exists, remove that too to have it regenerated.
    if len(dirs_to_remove_summary_files) > 0:
        # First, get rid of duplicate listings of directories where results files have been deleted.
        dirs_to_remove_summary_files = list(set(dirs_to_remove_summary_files))

        for dirname in dirs_to_remove_summary_files:
            # First, remove any files that contain the phrase "summary_results" in the title.
            summary_files = [
                os.path.join(dirname, fn)
                for fn in os.listdir(dirname)
                if fn.find("summary_results") > -1
            ]

            for fn in summary_files:
                if os.path.exists(fn):
                    files_removed.append(fn)
                    os.remove(fn)
                    if verbose:
                        print(os.path.basename(fn), "deleted.")

            # Now, find any "_results.h5" files that contain a base that matches a sub-directory name in the directory path.
            # This is because most summary results are akin to "CNMI_results.h5" where "CNMI" is a parent folder somewhere up the directory tree.
            # Look for these.
            path_parts = [dn for dn in dirname.split(os.sep) if len(dn) > 0]

            results_h5s = [
                fn for fn in os.listdir(dirname) if fn.find("_results.h5") > -1
            ]

            for r_h5 in results_h5s:
                if r_h5[: r_h5.find("_results.h5")] in path_parts:
                    h5_path = os.path.join(dirname, r_h5)
                    files_removed.append(h5_path)
                    os.remove(h5_path)
                    if verbose:
                        print(os.path.basename(h5_path), "deleted.")

    return files_removed


def check_for_and_remove_bad_granules_after_validation(dirname, verbose=True):
    """After a dataset has been validated (or even just partially validated) against ICESat-2
    with photon_results included, go back and check whether any granules contain bad data.
    If so, append them to the bad-granules csv, delete all the results that contain any bad
    granules. After this, the analysis can be re-run without the bad data and results
    re-computed.

    This is kinda the "do-it-all" functino for this module."""
    # if type(dataset_name_or_obj) == str:
    #     dset = datasets.etopo_source_dataset.get_source_dataset_object(dataset_name_or_obj)
    # else:
    #     assert isinstance(dataset_name_or_obj, datasets.etopo_source_dataset.ETOPO_source_dataset)
    #     dset = dataset_name_or_obj

    find_bad_granules_in_a_dataset(
        dirname,
        make_histogram_plots_if_bad=True,
        # results_subdir="icesat2_results",
        verbose=verbose,
    )

    create_master_list_of_bad_granules(dirname, append=True, verbose=verbose)

    list_of_bad_granules = get_list_of_granules_to_reject()
    if len(list_of_bad_granules) > 0 and verbose:
        print(len(list_of_bad_granules), "granules found to be deleted.")

    # dirname_list = [os.path.join("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/", d) \
    #                 for d in \
    #                 os.listdir("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/") \
    #                     if os.path.isdir(os.path.join("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/", d))]

    deleted_list = delete_results_with_bad_granules(
        dirname, dem_regex=r"_egm2008\.tif\Z", verbose=verbose
    )

    if verbose:
        print(len(deleted_list), "results files deleted.")

    return deleted_list


def read_and_parse_args():
    parser = argparse.ArgumentParser(
        description="Look through photon validation results, pick out any bad ICESat-2 granules, and delete result datafiles that relied on those granules."
    )
    parser.add_argument(
        "dirname", help="Directory to find '_photon_level_results.h5' files."
    )
    parser.add_argument(
        "--plot_all_histograms",
        "-p",
        action="store_true",
        default=False,
        help="Plot all the stacked histograms for each granule in the dataset.",
    )
    # parser.add_argument("NAME", help="Dataset name to check results for bad granules. Should be an ETOPO source dataset object.")
    return parser.parse_args()


if __name__ == "__main__":
    # symlink_bad_granule_plots_to_histogram_dir('/home/mmacferrin/Research/DATA/ETOPO/data/validation_results/15s/2022.09.29/plots',
    #                                            '/home/mmacferrin/Research/DATA/ETOPO/data/validation_results/15s/2022.09.29/bad_granules')
    args = read_and_parse_args()

    check_for_and_remove_bad_granules_after_validation(args.dirname)

    # gr = get_list_of_granules_to_reject(refind_bad_granules = True, regenerate_bad_granule_csv = True)
    # if len(gr) > 0:
    #     print("GRANULES TO EXCLUDE")
    #     for g in gr:
    #         print("\t", g)

    # 1. Find the bad granules in a dataset.
    # find_bad_granules_in_a_dataset("CUDEM_CONUS")

    # 2. Put them into the master bad_granules.csv list.
    # create_master_list_of_bad_granules("CUDEM_CONUS")

    # Test to see here what results we get back based on our filters.
    # print(get_list_of_granules_to_reject())

    # 3. Delete files that contain data from the bad granules.
    # dirname_list = [os.path.join("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/", d) \
    # for d in \
    # os.listdir("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/") \
    # if os.path.isdir(os.path.join("/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/", d))]
    # for dn in dirname_list:
    # print("===", os.path.split(dn)[1], "===")
    # delete_results_with_bad_granules(dn)

    # gids = find_granules_that_are_separate_from_others(\
    #        "/home/mmacferrin/Research/DATA/DEMs/CUDEM/data/CONUS/NCEI_ninth_Topobathy_2014_8483/AL_nwFL/ncei19_n30X50_w085X25_2019v1.tif",
    #        dem_vdatum="navd88",
    #        verbose=True)
