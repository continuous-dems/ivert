# -*- coding: utf-8 -*-

import numpy
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import matplotlib
import pandas
import os
import collections
import six
import math
import typing

####################################3
# Include the base /src/ directory of thie project, to add all the other modules.
# import import_parent_dir; import_parent_dir.import_src_dir_via_pythonpath()
####################################3
import utils.configfile
import utils.progress_bar

ivert_config = utils.configfile.Config()

def is_iterable(obj):
    """Tell whether an object is a non-string iterable. (list, tuple, etc)."""
    return (isinstance(obj, collections.abc.Iterable)
            and not isinstance(obj, six.string_types))

def get_data_from_h5_or_list(h5_name_or_list: typing.Union[str, typing.List[str]],
                             orig_filenames: typing.Union[None, str, typing.List[str]] = None,
                             empty_val: float = ivert_config.dem_default_ndv,
                             include_filenames: bool = False,
                             verbose: bool = True) -> pandas.DataFrame:
    """Return the data either from a single hdf5 results file, or a list of them. Filter out empty (bad data) values."""
    if type(h5_name_or_list) is str:
        data = pandas.read_hdf(h5_name_or_list)
        if include_filenames:
            if orig_filenames is None:
                data["filename"] = os.path.basename(h5_name_or_list)
            else:
                assert type(orig_filenames) is str
                data["filename"] = os.path.basename(orig_filenames)

    elif is_iterable(h5_name_or_list):

        if verbose:
            print("Reading {0} h5 results files.".format(len(h5_name_or_list)))
        data_list = []

        for i, h5_file in enumerate(h5_name_or_list):
            if os.path.exists(h5_file):
                temp_data = pandas.read_hdf(h5_file)
                if include_filenames:
                    if orig_filenames is None:
                        temp_data["filename"] = os.path.basename(h5_file)
                    else:
                        assert is_iterable(orig_filenames)
                        temp_data["filename"] = os.path.basename(orig_filenames[i])

                data_list.append(temp_data)

            if verbose:
                utils.progress_bar.ProgressBar(i + 1, len(h5_name_or_list),
                                               suffix='{0}/{1}'.format(i + 1, len(h5_name_or_list)))

        data = pandas.concat(data_list)
    else:
        raise TypeError("Non-iterable value for parameter 'results_h5_name_or_list':", h5_name_or_list)
    # print(data)

    # cellstd         = data['stddev'].astype(float)
    # meandiff        = data['diff_mean']
    # # numphotons      = data['numphotons']
    # numphotons_intd = data['numphotons_intd']
    # canopy_fraction = data["canopy_fraction"]
    # Plot some histograms

    # Get rid of data with 3 or less ground photons in the inter-decile range, and all nans.
    # If empty_val means something, return it.
    # good_data_mask = (numphotons_intd > 3) & ~numpy.isnan(meandiff) & ~numpy.isnan(meddiff) & ~numpy.isnan(canopy_fraction) \
    #                  & (meandiff != empty_val) & (canopy_fraction != empty_val) & (cellstd != empty_val)

    # data_subset = data[good_data_mask].copy()
    if (len(data) == 0) and verbose:
        print("No reliable results contained in list of results h5 files.")

    return data

def plot_histograms_and_line(results_h5_or_list_or_df,
                             output_figure_name,
                             empty_val = ivert_config.dem_default_ndv,
                             place_name=None,
                             figsize = (10.0, 4.0), # Width/height, in inches
                             labels_uppercase = True,
                             dpi = 600,
                             hist_cutoff_num_stddevs = 2.5,
                             also_add_rmse_to_hist = False,
                             verbose=True):
    """Generate a 4-panel figure of error stats.
    1) Histograms of mean errors land-only (green)
    2) Histogram of mean errors bathy (blue)
    3) 1:1 line of DEM vs ICESat-2 elevationss

    If 'place_name' is provided, use it in the title of the plot.
    """
    # If we're writing a PNG file, use the "Agg" backend (no display).
    # This helps avoid errors.
    if os.path.splitext(output_figure_name)[1].lower() == ".png":
        matplotlib.use("Agg")

    if type(results_h5_or_list_or_df) == pandas.DataFrame:
        data = results_h5_or_list_or_df
    else:
        data = get_data_from_h5_or_list(results_h5_or_list_or_df,
                                        empty_val = empty_val)

    meandiff        = data['diff_mean']
    # numphotons      = data['numphotons']
    # numphotons_intd = data['numphotons_intd']
    numphotons_bathy = data['numphotons_bathy']
    dem_elev        = data["dem_elev"]
    mean_elev       = data["mean"]

    if len(meandiff) < 3:
        if verbose:
            print("Not enough cells to plot statistics. Aborting.")
        return

    # Determine which histogram panels have data before creating the figure.
    land_only_mask = (numphotons_bathy == 0)
    meandiff_land = meandiff[land_only_mask]
    bathy_mask = (numphotons_bathy > 0)
    meandiff_bathy = meandiff[bathy_mask]
    has_land = len(meandiff_land) > 0
    has_bathy = len(meandiff_bathy) > 0

    # ncols = number of histogram panels present + 1 scatter panel.
    ncols = int(has_land) + int(has_bathy) + 1

    # Generate figure. Scale width proportionally to panel count.
    if figsize is None:
        figsize = matplotlib.rcParams['figure.figsize']
    scaled_figsize = (figsize[0] * ncols / 3, figsize[1])
    fig, axes = plt.subplots(1, ncols, dpi=dpi, figsize=scaled_figsize, tight_layout=True)
    if ncols == 1:
        axes = [axes]

    plot_label_margin = [0.015, 0.97]
    plot_label_ha = "left"
    plot_label_va = "top"
    plot_label_size = "large"
    plot_label_weight = "book"

    panel_letters = ("ABCDE" if labels_uppercase else "abcde")
    panel_idx = 0

    #############################################################################
    # Plot: Histogram of differences from ICESat-2 mean (land only), if present.
    if has_land:
        nbins = 200
        ax1 = axes[panel_idx]
        panel_letter = panel_letters[panel_idx]
        panel_idx += 1

        ax1.hist(meandiff_land, bins=nbins, color="darkred")
        # Unicode "minus" sign is \u2212
        ax1.set_title("DEM " + u"\u2212" + " ICESat-2 elevation: land")
        ax1.set_ylabel("% of data cells")
        ax1.set_xlabel("Elevation difference (m)")
        ax1.yaxis.set_major_formatter(ticker.PercentFormatter(max(len(meandiff_land), 1), decimals=0))

        # Add the lines for mean +- std
        center = numpy.mean(meandiff_land)
        std = numpy.std(meandiff_land)
        ax1.axvline(x=center, color="black", linewidth=0.75)
        ax1.axvline(x=center+std, color="black", linestyle="--", linewidth=0.5)
        ax1.axvline(x=center-std, color="black", linestyle="--", linewidth=0.5)

        # Crop the left & right (only if greater than 20 points)
        if len(meandiff_land) >= 20:
            cutoffs = numpy.percentile(meandiff_land, [1, 99])
        else:
            cutoffs = [min(meandiff_land), max(meandiff_land)]

        # If we have a zero-width range, arbitrarily buffer it by 1 m in each direction.
        if cutoffs[0] == cutoffs[1]:
            cutoffs[0] = cutoffs[0] - 1
            cutoffs[1] = cutoffs[1] + 1

        # Do not crop the photo to make the stddev lines fall outside the plot.
        # If they do, reset the min/max cutoff to be 2 stddev away from the mean on that side.
        if (center + std) >= cutoffs[1] or hist_cutoff_num_stddevs is not None:
            cutoffs[1] = center + (std * hist_cutoff_num_stddevs)
        if (center - std) <= cutoffs[0] or hist_cutoff_num_stddevs is not None:
            cutoffs[0] = center - (std * hist_cutoff_num_stddevs)

        # Just error checking, if any of the cutoffs come back with NaN or Inf, just clip it to -1, 1, debug later.
        if numpy.any(numpy.isnan(cutoffs) | numpy.isinf(cutoffs)):
            cutoffs = [-1, 1]

        ax1.set_xlim(cutoffs)

        # Pad the top by 10%
        ylim = ax1.get_ylim()
        ax1.set_ylim((ylim[0], ylim[1] * 1.1))

        txt = ax1.text(0.11, 0.95,
                       r"{0:.2f} $\pm$ {1:.2f} m".format(center, std),
                       ha="left", va="top", fontsize="small",
                       transform=ax1.transAxes)
        txt.set_bbox(dict(facecolor="white", alpha=0.85, edgecolor="white", boxstyle="square,pad=0"))

        # If requested, add the RMSE value to the figure.
        if also_add_rmse_to_hist:
            rmse = numpy.sqrt(numpy.mean(meandiff_land ** 2))
            txt_std = ax1.text(0.97, 0.95,
                               "RMSE: {0:0.2f} m".format(rmse),
                               ha="right", va="top", fontsize="small",
                               transform=ax1.transAxes)
            txt_std.set_bbox(dict(facecolor="white", alpha=0.95, edgecolor="white", boxstyle="square,pad=0"))

        ax1.text(*plot_label_margin,
                 panel_letter,
                 ha=plot_label_ha, va=plot_label_va, fontsize=plot_label_size,
                 fontweight=plot_label_weight, transform=ax1.transAxes)

    #############################################################################
    # Plot: Histogram of differences from ICESat-2 mean (bathy only), if present.
    if has_bathy:
        nbins = 100  # fewer bins than land histogram
        ax2 = axes[panel_idx]
        panel_letter = panel_letters[panel_idx]
        panel_idx += 1

        ax2.hist(meandiff_bathy, bins=nbins, color="blue")
        # Unicode "minus" sign is \u2212
        ax2.set_title("DEM " + u"\u2212" + " ICESat-2 elevation: bathy")
        ax2.set_ylabel("% of data cells")
        ax2.set_xlabel("Elevation difference (m)")
        ax2.yaxis.set_major_formatter(ticker.PercentFormatter(max(len(meandiff_bathy), 1), decimals=0))

        # Add the lines for mean +- std
        center = numpy.mean(meandiff_bathy)
        std = numpy.std(meandiff_bathy)
        ax2.axvline(x=center, color="black", linewidth=0.75)
        ax2.axvline(x=center+std, color="black", linestyle="--", linewidth=0.5)
        ax2.axvline(x=center-std, color="black", linestyle="--", linewidth=0.5)

        # Crop the left & right (only if greater than 20 points)
        if len(meandiff_bathy) >= 20:
            cutoffs = numpy.percentile(meandiff_bathy, [1, 99])
        else:
            cutoffs = [min(meandiff_bathy), max(meandiff_bathy)]

        # If we have a zero-width range, arbitrarily buffer it by 1 m in each direction.
        if cutoffs[0] == cutoffs[1]:
            cutoffs[0] = cutoffs[0] - 1
            cutoffs[1] = cutoffs[1] + 1

        # Do not crop the photo to make the stddev lines fall outside the plot.
        # If they do, reset the min/max cutoff to be 2 stddev away from the mean on that side.
        if (center + std) >= cutoffs[1] or hist_cutoff_num_stddevs is not None:
            cutoffs[1] = center + (std * hist_cutoff_num_stddevs)
        if (center - std) <= cutoffs[0] or hist_cutoff_num_stddevs is not None:
            cutoffs[0] = center - (std * hist_cutoff_num_stddevs)

        # Just error checking, if any of the cutoffs come back with NaN or Inf, just clip it to -1, 1, debug later.
        if numpy.any(numpy.isnan(cutoffs) | numpy.isinf(cutoffs)):
            cutoffs = [-1, 1]

        ax2.set_xlim(cutoffs)

        # Pad the top by 10%
        ylim = ax2.get_ylim()
        ax2.set_ylim((ylim[0], ylim[1] * 1.1))

        txt = ax2.text(0.11, 0.95,
                       r"{0:.2f} $\pm$ {1:.2f} m".format(center, std),
                       ha="left", va="top", fontsize="small",
                       transform=ax2.transAxes)
        txt.set_bbox(dict(facecolor="white", alpha=0.85, edgecolor="white", boxstyle="square,pad=0"))

        # If requested, add the RMSE value to the figure.
        if also_add_rmse_to_hist:
            rmse = numpy.sqrt(numpy.mean(meandiff_bathy ** 2))
            txt_std = ax2.text(0.97, 0.95,
                               "RMSE: {0:0.2f} m".format(rmse),
                               ha="right", va="top", fontsize="small",
                               transform=ax2.transAxes)
            txt_std.set_bbox(dict(facecolor="white", alpha=0.95, edgecolor="white", boxstyle="square,pad=0"))

        ax2.text(*plot_label_margin,
                 panel_letter,
                 ha=plot_label_ha, va=plot_label_va, fontsize=plot_label_size,
                 fontweight=plot_label_weight, transform=ax2.transAxes)

    # Plot: 1:1 line of DEM/ICESat-2 elevations (always present).
    #############################################################################
    ax3 = axes[panel_idx]
    panel_letter = panel_letters[panel_idx]

    dotsize=3
    # Adjust the alpha depending how many points there are (more points == lighter dots)
    alpha = 0.35 * max(0.0025, min(4, (math.log10(100)/math.log10(len(mean_elev)))))
    # Can't have an alpha > 1.0, so cap it there.
    alpha = min(alpha, 1.0)

    # Scatter plots of both land and elev
    ax3.scatter(mean_elev[land_only_mask], dem_elev[land_only_mask], c="darkred", s=dotsize, linewidth=0, alpha=alpha)
    ax3.scatter(mean_elev[~land_only_mask], dem_elev[~land_only_mask], c="blue", s=dotsize, linewidth=0, alpha=min(alpha * 2, 0.35))
    ax3.set_title("DEM vs. ICESat-2")
    ax3.set_ylabel("DEM elevation (m)")
    ax3.set_xlabel("ICESat-2 elevation (m)")
    xlim = ax3.get_xlim()
    ylim = ax3.get_ylim()

    plotlim =( min(xlim[0], ylim[0]), max(xlim[1],ylim[1]))
    ax3.set_xlim(plotlim)
    ax3.set_ylim(plotlim)
    ax3.plot(plotlim, plotlim, ls="--", c=".3", lw=0.5, alpha=0.6)
    # Set the y-ticks the same as the x-ticks.
    xticks = ax3.get_xticks()
    ax3.set_yticks(xticks)

    ax3.text(*plot_label_margin,
             panel_letter,
             ha=plot_label_ha,
             va=plot_label_va,
             fontsize=plot_label_size,
             fontweight=plot_label_weight,
             transform=ax3.transAxes)
    # lbltxt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))

    # Figure title
    if place_name is None:
        place_name = "DEM"

    rmse = (numpy.sum(meandiff ** 2) / len(meandiff)) ** 0.5

    fig.suptitle(f"{place_name}: Errors and Distributions\nRMSE = {rmse:0.3f} m,   N = {len(meandiff):,} cells")
    fig.tight_layout()

    # Save the figure to disk.
    fig.savefig(output_figure_name)
    if verbose:
        print(output_figure_name, "written.")

        # Compute the RMSE and spit that out too.
        print("\tRMSE: {0:0.3f} m".format(rmse))

    # Clear the figure and close the plot.
    # If the plot is not "plt.close()"'ed, MatPlotLib keeps it in memory indefinitely
    # even after it's no longer referenced, which is... annoying. Gotta close it explicitly here.
    plt.clf()
    plt.close(fig)

    return


def plot_histogram_and_error_stats_4_panels(results_h5_or_list_or_df,
                                            output_figure_name,
                                            empty_val = ivert_config.dem_default_ndv,
                                            place_name=None,
                                            figsize=None,
                                            labels_uppercase = True,
                                            # error_max_cutoff = None,
                                            dpi = 600,
                                            hist_cutoff_num_stddevs = 2,
                                            also_add_rmse_to_hist = False,
                                            verbose=True):
    """Generate a 4-panel figure of error stats.
    1) Histograms of mean errors
    2) 1:1 line of DEM vs ICESat-2 elevations.
    3) Histogram of canopy cover
    4) Histogram of # of photons per grid cell.

    If 'place_name' is provided, use it in the title of the plot.
    """
    # If we're writing a PNG file, use the "Agg" backend (no display).
    # This helps avoid errors.
    if os.path.splitext(output_figure_name)[1].lower() == ".png":
        matplotlib.use("Agg")

    if type(results_h5_or_list_or_df) == pandas.DataFrame:
        data = results_h5_or_list_or_df
    else:
        data = get_data_from_h5_or_list(results_h5_or_list_or_df,
                                        empty_val = empty_val)

    # meddiff         = data['diff_median']
    # cellstd         = data['stddev'].astype(float)
    meandiff        = data['diff_mean']
    # numphotons      = data['numphotons']
    numphotons_intd = data['numphotons_intd']
    canopy_fraction = data["canopy_fraction"]
    dem_elev        = data["dem_elev"]
    mean_elev       = data["mean"]

    # TODO: Get rid of these comments after testing. We already filtered data out after validation, don't need to do it
    #   again here.
    # Get rid of data with 3 or less ground photons in the inter-decile range, and all nans.
    # good_data_mask = numphotons_intd > 3 & ~numpy.isnan(meandiff) & ~numpy.isnan(canopy_fraction)

    # Filter out errors that are above a certain threshold, if provided (presumed to be ICESat-2 anomalies, such as in the CUDEM tiles.)
    # if error_max_cutoff is not None:
    #     good_data_mask = good_data_mask & (numpy.abs(meandiff) <= error_max_cutoff)

    # meandiff           =        meandiff[good_data_mask]
    # # meddiff            =         meddiff[good_data_mask]
    # # cellstd            =         cellstd[good_data_mask]
    # canopy_fraction    = canopy_fraction[good_data_mask]
    # # numphotons         =      numphotons[good_data_mask]
    # numphotons_intd    = numphotons_intd[good_data_mask]
    # dem_elev           =        dem_elev[good_data_mask]
    # mean_elev          =       mean_elev[good_data_mask]


    if len(meandiff) < 3:
        if verbose:
            print("Not enough cells to plot statistics. Aborting.")
        return

    # Generate figure. If a figure size isn't given, use the matplotlib.rcParams default.
    if figsize is None:
        figsize = matplotlib.rcParams['figure.figsize']
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2,2, dpi=dpi, figsize=figsize, tight_layout=True)

    plot_label_margin = [0.015, 0.97]
    plot_label_ha = "left"
    plot_label_va = "top"
    plot_label_size = "large"
    plot_label_weight = "book"

    nbins = 1000
    #############################################################################
    # Plot 1, differences from iceast-2 mean.
    ax1.hist(meandiff, bins=nbins)
    # Unicode "minus" sign is \u2212
    ax1.set_title("DEM " + u"\u2212" + " ICESat-2 elevation")
    ax1.set_ylabel("% of data cells")
    ax1.set_xlabel("Elevation difference (m)")
    ax1.yaxis.set_major_formatter(ticker.PercentFormatter(max(len(meandiff), 1), decimals=0))

    # Add the lines for mean +- std
    center = numpy.mean(meandiff)
    std = numpy.std(meandiff)
    ax1.axvline(x=center, color="darkred", linewidth=0.75)
    ax1.axvline(x=center+std, color="darkred", linestyle="--", linewidth=0.5)
    ax1.axvline(x=center-std, color="darkred", linestyle="--", linewidth=0.5)

    # Crop the left & right
    cutoffs = numpy.percentile(meandiff, [1, 99])
    # cutoffs = [-max(numpy.abs(cutoffs)), max(numpy.abs(cutoffs))]
    # Do not crop the photo to make the stddev lines fall outside the plot.
    # If they do, reset the min/max cutoff to be 2 stddev away from the mean on that side.
    if (center + std) >= cutoffs[1] or hist_cutoff_num_stddevs is not None:
        cutoffs[1] = center + (std * hist_cutoff_num_stddevs)
    if (center - std) <= cutoffs[0] or hist_cutoff_num_stddevs is not None:
        cutoffs[0] = center - (std * hist_cutoff_num_stddevs)
    ax1.set_xlim(cutoffs)

    # Detect whether the mean line is closer to the left or the right (we'll put the text box on the other side)
    # (In this case, since we're putting sub-panel letters in the upper-left, we'll just always put the other text in the upper-right.
    text_left = False # not ((center - cutoffs[0]) < (cutoffs[1] - center))
    txt = ax1.text(0.11, # if text_left else 0.97,
                   0.95, # 0.85 if text_left else 0.95,
                   r"{0:.2f} $\pm$ {1:.2f} m".format(center, std),
                   ha="left", # if text_left else "right",
                   va="top",
                   fontsize="small",
                   transform=ax1.transAxes)
    txt.set_bbox(dict(facecolor="white", alpha=0.85, edgecolor="white", boxstyle="square,pad=0"))

    # If requested, as the RMSE value to the figure.
    if also_add_rmse_to_hist:
        rmse = numpy.sqrt(numpy.mean(meandiff ** 2))
        txt_std = ax1.text(0.97, #0.12 if text_left else 0.97,
                           0.95,
                           "RMSE: {0:0.2f} m".format(rmse),
                           ha="right", #"left" if text_left else "right",
                           va="top",
                           fontsize="small",
                           transform=ax1.transAxes)
        txt_std.set_bbox(dict(facecolor="white", alpha=0.85, edgecolor="white", boxstyle="square,pad=0"))

    # Add subplot label "a"
    # Add it on the opposite (horizontal) side of wherever the other text has been placed, in this case.
    # if text_left:
    #     ax1_label_x = plot_label_margin[0] + 0.15
    #     ax1_ha = "right"
    # else:
    #     ax1_label_x = plot_label_margin[0]
    #     ax1_ha = "left"
    ax1.text(*plot_label_margin,
             "A" if labels_uppercase else "a",
             ha = plot_label_ha,
             va = plot_label_va,
             fontsize = plot_label_size,
             fontweight = plot_label_weight,
             transform = ax1.transAxes)
    # lbltxt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))

    # 2) Plot 1:1 line of DEM/ICESat-2 elevations.
    #############################################################################
    # Subplot 2, elev-elev correlation line
    dotsize=2
    alpha = 0.25 * max(0.0025, min(4, (math.log10(100)/math.log10(len(mean_elev)))))
    ax2.scatter(mean_elev, dem_elev, s=dotsize, linewidth=0, alpha=alpha)
    ax2.set_title("DEM vs. ICESat-2")
    ax2.set_ylabel("DEM elevation (m)")
    ax2.set_xlabel("ICESat-2 elevation (m)")
    # ax2.autoscale(False) # Keep the line-plotting from expanding the x,y-axes
    xlim = ax2.get_xlim()
    ylim = ax2.get_ylim()

    plotlim =( min(xlim[0], ylim[0]), max(xlim[1],ylim[1]))
    ax2.set_xlim(plotlim)
    ax2.set_ylim(plotlim)
    ax2.plot(plotlim, plotlim, ls="--", c=".3", lw=0.5, alpha=0.6)
    # ax2.plot(xlim, xlim, ls="--", c=".3", lw=1, alpha=0.6)
    # Set the y-ticks the same as the x-ticks.
    xticks = ax2.get_xticks()
    ax2.set_yticks(xticks)

    # Add subplot label "b"
    ax2.text(*plot_label_margin,
             "B"  if labels_uppercase else "b",
             ha=plot_label_ha,
             va=plot_label_va,
             fontsize=plot_label_size,
             fontweight=plot_label_weight,
             transform=ax2.transAxes)
    # lbltxt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))


    # 3) Plot Histogram of # of ICESat-2 photons per cell.
    #############################################################################
    # Plot 4, number of photons in interdecile range
    ax3.hist(numphotons_intd, bins=min((numphotons_intd.max() - numphotons_intd.min() + 1), 200), color="darkred")
    ax3.set_title("Number of photons")
    ax3.set_xlabel("Photon count per DEM cell")
    ax3.set_ylabel("% of data cells")
    ax3.yaxis.set_major_formatter(ticker.PercentFormatter(max(len(numphotons_intd), 1), decimals=1))

    # Crop the left & right, right at 98 percentile.
    cutoff = numpy.percentile(numphotons_intd, 99)
    xmin = ax3.get_xlim()[0]
    ax3.set_xlim(xmin*0.5, cutoff)

    center = numpy.mean(numphotons_intd)
    std = numpy.std(numphotons_intd)

    ax3.text(0.95, 0.95, "{0:d} $\\pm$ {1:d}\nphotons per cell".format(int(numpy.round(center)), int(numpy.round(std))),
             ha="right", va="top",
             fontsize="small",
             transform=ax3.transAxes)

    # Add subplot label "c"
    ax3.text(*plot_label_margin,
             "C"  if labels_uppercase else "c",
             ha=plot_label_ha,
             va=plot_label_va,
             fontsize=plot_label_size,
             fontweight=plot_label_weight,
             transform=ax3.transAxes)
    # lbltxt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))

    # 4) Plot Histogram of canopy cover.
    #############################################################################
    # Plot 3, percent canopy cover
    canopy_fraction = (canopy_fraction * 100).astype(float)
    ax4.hist(canopy_fraction, bins=50,color="darkgreen")
    ax4.set_title("Canopy Cover (%)")
    ax4.set_xlabel("% Canopy Cover")
    ax4.yaxis.set_major_formatter(ticker.PercentFormatter(max(len(canopy_fraction), 1), decimals=0))

    # Crop the right edge at the 99th percentile
    cutoff = numpy.percentile(canopy_fraction, 99)
    # Add just a bit of padding on the left to make more room for the "D" label.
    xmin = ax4.get_xlim()[0] - 0.025 * (cutoff - ax4.get_xlim()[0])
    ax4.set_xlim(xmin, cutoff)

    # center = numpy.mean(canopy_fraction)
    # # std = numpy.std(canopy_fraction)
    # median = numpy.median(canopy_fraction)
    canopy_mask = (canopy_fraction > 0.0) & numpy.isfinite(canopy_fraction) & ~numpy.isnan(canopy_fraction)

    ax4.text(0.95, 0.95, "{0:0.1f} % of cells have >0 cover:\n{1:0.1f} $\\pm$ {2:0.1f} % canopy cover\nin non-zero cells".format( \
                         numpy.count_nonzero(canopy_mask) * 100 / canopy_fraction.size,
                         numpy.mean(canopy_fraction[canopy_mask]),
                         numpy.std(canopy_fraction[canopy_mask])),
             ha="right", va="top", transform=ax4.transAxes,
             fontsize="small")

    # Add subplot label "d"
    ax4.text(*plot_label_margin,
             "D"  if labels_uppercase else "d",
             ha=plot_label_ha,
             va=plot_label_va,
             fontsize=plot_label_size,
             fontweight=plot_label_weight,
             transform=ax4.transAxes)
    # lbltxt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))


    # Figure title
    if place_name is None:
        place_name = "DEM"
    fig.suptitle("{0}: Errors and Distributions\n(N = {1:,} cells)".format(place_name, len(meandiff)))
    fig.tight_layout()

    # Save the figure to disk.
    fig.savefig(output_figure_name)
    if verbose:
        print(output_figure_name, "written.")

        # Compute the RMSE and spit that out too.
        rmse = (numpy.sum(meandiff ** 2) / len(meandiff)) ** 0.5
        print("\tRMSE: {0:0.3f} m".format(rmse))

    # Clear the figure and close the plot.
    # If the plot is not "plt.close()"'ed, MatPlotLib keeps it in memory indefinitely
    # even after it's no longer referenced, which is... annoying. Gotta close it explicitly here.
    plt.clf()
    plt.close(fig)

    return


# def plot_error_stats(results_h5_name_or_list, empty_val = ivert_config.dem_default_ndv):
#     # print(fname)
#     data = get_data_from_h5_or_list(results_h5_name_or_list, empty_val = empty_val)
#
#     meddiff         = data['diff_median']
#     cellstd         = data['stddev'].astype(float)
#     meandiff        = data['diff_mean']
#     numphotons      = data['numphotons']
#     numphotons_intd = data['numphotons_intd']
#     canopy_fraction = data["canopy_fraction"]
#     dem_elev        = data["dem_elev"]
#     mean_elev       = data["mean"]
#     # Plot some histograms
#
#     # Get rid of data with 3 or less ground photons in the inter-decile range, and all nans.
#     good_data_mask = numphotons_intd > 3 & ~numpy.isnan(meandiff) & ~numpy.isnan(meddiff) & ~numpy.isnan(canopy_fraction)
#     meandiff           =        meandiff[good_data_mask]
#     meddiff            =         meddiff[good_data_mask]
#     cellstd            =         cellstd[good_data_mask]
#     canopy_fraction    = canopy_fraction[good_data_mask]
#     numphotons         =      numphotons[good_data_mask]
#     numphotons_intd    = numphotons_intd[good_data_mask]
#     dem_elev           =        dem_elev[good_data_mask]
#     mean_elev          =       mean_elev[good_data_mask]
#
#     fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2,2, dpi=600, tight_layout=True) #, sharey=True)
#
#     dotsize=1
#
#     #############################################################################
#     # Subplot 1, elev-elev correlation line
#     ax1.scatter(mean_elev, dem_elev, s=dotsize)
#     ax1.set_ylabel("DEM elevation (m)")
#     ax1.set_xlabel("ICESat-2 elevation (m)")
#     xlim = ax1.get_xlim()
#     ax1.autoscale(False) # Keep the line-plotting from expanding the x,y-axes
#     ax1.plot(xlim, xlim, ls="--", c=".3", lw=1)
#
#     #############################################################################
#     # Subplot 2, error vs icesat-2 elevation
#     ax2.scatter(dem_elev, meandiff, s=dotsize, color="darkred")
#     ax2.axhline(y=0, ls="--", c=".3", lw=1)
#     ax2.set_xlabel("ICESat-2 elev (m)")
#     ax2.set_ylabel("DEM error (m)")
#
#     ax3.scatter(canopy_fraction*100, meandiff, s=dotsize, color="darkgreen")
#     ax3.axhline(y=0, ls="--", c=".3", lw=1)
#     ax3.set_xlabel("Canopy Cover (%)")
#     ax3.set_ylabel("DEM error (m)")
#
#     ax4.scatter(numphotons_intd, meandiff, s=dotsize, color="purple")
#     ax4.axhline(y=0, ls="--", c=".3", lw=1)
#     ax4.set_xlabel("# photons per cell")
#     ax4.set_ylabel("DEM errors (m)")
#
#
#     # fig.suptitle(figure_titles_dict[fname] + " Error Stats")
#     fig.suptitle("NE DEM Error Stats\n(N = {0:,} cells)".format(len(data)))
#     fig.tight_layout()
#
#
#     # figname = os.path.splitext(fname)[0] + "_stats.png"
#     figname = os.path.join(os.path.split(results_h5_name_or_list[0])[0], os.path.split(os.path.split(results_h5_name_or_list[0])[0])[1]) + "_stats.png"
#     fig.savefig(figname)
#     print(figname, "written.")
#
#     plt.clf()

# if __name__ == '__main__':
#
#     for h5 in [os.path.join('/home/mmacferrin/Research/DATA/ETOPO/data/validation_results/15s/2022.09.29/plots/', fn) for fn in
#                ["total_results_gt{0}.h5".format(i) for i in (list(range(40,48)) + [50])]]:
#         #, "total_results_gt42.h5", "total_results_gt43.h5", "total_results_gt44.h5", "total_results_gt46.h5"]]:
#         plotname = os.path.splitext(h5)[0] + "_plot.png"
#
#         plot_histogram_and_error_stats_4_panels(h5, plotname, place_name="ETOPO 2022")

    # plot_histograms(h5_names)
    # plot_error_stats(h5_names)
    # for fname in fnames:
    #     print("\n====", fname)
    #     plot_histograms(fname)
    #     plot_error_stats(fname)
    #     # input("<Press <Enter> to continue>")


if __name__ == '__main__':
    results_df = pandas.read_hdf("/home/mmacferrin/Research/DEMs/CUDEMs_1_9_Oregon_2025/dems/2025.10.10_w_metadata/icesat2/ncei19_n45x50_w124x00_2025v1_results.h5")
    plot_histograms_and_line(results_df,
                             output_figure_name="/home/mmacferrin/Research/DEMs/CUDEMs_1_9_Oregon_2025/dems/2025.10.10_w_metadata/icesat2/ncei19_n45x50_w124x00_2025v1_plot.png",
                             figsize=(10.0, 3.3),
                             dpi=600,
                             place_name="ncei19_n45x50_w124x00_2025v1",
                             hist_cutoff_num_stddevs = 2.5,
                             also_add_rmse_to_hist=True,
                             verbose=True)