"""Plot histograms and error statistics from IVERT validation results."""

import collections
import logging
import math
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tqdm
from matplotlib import ticker

from ivert.utils import dem_source

logger = logging.getLogger(__name__)


def is_iterable(obj):
    """Tell whether an object is a non-string iterable. (list, tuple, etc)."""
    return isinstance(obj, collections.abc.Iterable) and not isinstance(
        obj,
        str,
    )


def get_data_from_h5_or_list(
    h5_name_or_list: str | Path | list[str | Path],
    orig_filenames: str | list[str] | None = None,
    include_filenames: bool = False,
) -> pd.DataFrame:
    """Return the data either from a single hdf5 results file, or a list of them. Filter out empty (bad data) values."""
    if isinstance(h5_name_or_list, (str, Path)):
        data = pd.read_hdf(h5_name_or_list)
        if include_filenames:
            if orig_filenames is None:
                data["filename"] = Path(h5_name_or_list).name
            else:
                if not isinstance(orig_filenames, str):
                    msg = "orig_filenames must be a string when h5_name_or_list is one."
                    raise TypeError(msg)
                data["filename"] = dem_source.dem_display_name(orig_filenames)

    elif is_iterable(h5_name_or_list):
        logger.info("Reading %s h5 results files.", len(h5_name_or_list))
        data_list = []

        # 'disable=None' tells tqdm to draw the bar only when attached to a terminal,
        # and stay silent when the output is redirected to a file or a pipe.
        for i, h5_file in enumerate(
            tqdm.tqdm(
                h5_name_or_list,
                disable=None if logger.isEnabledFor(logging.INFO) else True,
                unit="file",
            ),
        ):
            if Path(h5_file).exists():
                temp_data = pd.read_hdf(h5_file)
                if include_filenames:
                    if orig_filenames is None:
                        temp_data["filename"] = Path(h5_file).name
                    else:
                        if not is_iterable(orig_filenames):
                            msg = "orig_filenames must be a list when h5_name_or_list is one."
                            raise TypeError(msg)
                        temp_data["filename"] = dem_source.dem_display_name(
                            orig_filenames[i],
                        )

                data_list.append(temp_data)

        data = pd.concat(data_list)
    else:
        msg = "Non-iterable value for parameter 'results_h5_name_or_list':"
        raise TypeError(
            msg,
            h5_name_or_list,
        )
    if len(data) == 0:
        logger.info("No reliable results contained in list of results h5 files.")

    return data


def plot_histograms_and_line(
    results_h5_or_list_or_df,
    output_figure_name,
    *,
    place_name=None,
    figsize=(10.0, 4.0),  # Width/height, in inches
    labels_uppercase=True,
    dpi=600,
    hist_cutoff_num_stddevs=2.5,
    also_add_rmse_to_hist=False,
):
    """Generate a 4-panel figure of error stats.

    1) Histograms of mean errors land-only (green)
    2) Histogram of mean errors bathy (blue)
    3) 1:1 line of DEM vs ICESat-2 elevations

    If 'place_name' is provided, use it in the title of the plot.
    """
    # If we're writing a PNG file, use the "Agg" backend (no display).
    # This helps avoid errors.
    if Path(output_figure_name).suffix.lower() == ".png":
        mpl.use("Agg")

    if type(results_h5_or_list_or_df) is pd.DataFrame:
        data = results_h5_or_list_or_df
    else:
        data = get_data_from_h5_or_list(results_h5_or_list_or_df)

    meandiff = data["diff_mean"]
    numphotons_bathy = data["numphotons_bathy"]
    dem_elev = data["dem_elev"]
    mean_elev = data["mean"]

    if len(meandiff) < 3:
        logger.info("Not enough cells to plot statistics. Aborting.")
        return

    # Determine which histogram panels have data before creating the figure.
    land_only_mask = numphotons_bathy == 0
    meandiff_land = meandiff[land_only_mask]
    bathy_mask = numphotons_bathy > 0
    meandiff_bathy = meandiff[bathy_mask]
    has_land = len(meandiff_land) > 0
    has_bathy = len(meandiff_bathy) > 0

    # ncols = number of histogram panels present + 1 scatter panel.
    ncols = int(has_land) + int(has_bathy) + 1

    # Generate figure. Scale width proportionally to panel count.
    if figsize is None:
        figsize = mpl.rcParams["figure.figsize"]
    scaled_figsize = (figsize[0] * ncols / 3, figsize[1])
    fig, axes = plt.subplots(
        1,
        ncols,
        dpi=dpi,
        figsize=scaled_figsize,
        tight_layout=True,
    )
    if ncols == 1:
        axes = [axes]

    plot_label_margin = [0.015, 0.97]
    plot_label_ha = "left"
    plot_label_va = "top"
    plot_label_size = "large"
    plot_label_weight = "book"

    panel_letters = "ABCDE" if labels_uppercase else "abcde"
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
        ax1.set_title("DEM " + "\u2212" + " ICESat-2 elevation: land")
        ax1.set_ylabel("% of data cells")
        ax1.set_xlabel("Elevation difference (m)")
        ax1.yaxis.set_major_formatter(
            ticker.PercentFormatter(max(len(meandiff_land), 1), decimals=0),
        )

        # Add the lines for mean +- std
        center = np.mean(meandiff_land)
        std = np.std(meandiff_land)
        ax1.axvline(x=center, color="black", linewidth=0.75)
        ax1.axvline(x=center + std, color="black", linestyle="--", linewidth=0.5)
        ax1.axvline(x=center - std, color="black", linestyle="--", linewidth=0.5)

        # Crop the left & right (only if greater than 20 points)
        if len(meandiff_land) >= 20:
            cutoffs = np.percentile(meandiff_land, [1, 99])
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
        if np.any(np.isnan(cutoffs) | np.isinf(cutoffs)):
            cutoffs = [-1, 1]

        ax1.set_xlim(cutoffs)

        # Pad the top by 10%
        ylim = ax1.get_ylim()
        ax1.set_ylim((ylim[0], ylim[1] * 1.1))

        txt = ax1.text(
            0.11,
            0.95,
            rf"{center:.2f} $\pm$ {std:.2f} m",
            ha="left",
            va="top",
            fontsize="small",
            transform=ax1.transAxes,
        )
        txt.set_bbox(
            {
                "facecolor": "white",
                "alpha": 0.85,
                "edgecolor": "white",
                "boxstyle": "square,pad=0",
            },
        )

        # If requested, add the RMSE value to the figure.
        if also_add_rmse_to_hist:
            rmse = np.sqrt(np.mean(meandiff_land**2))
            txt_std = ax1.text(
                0.97,
                0.95,
                f"RMSE: {rmse:0.2f} m",
                ha="right",
                va="top",
                fontsize="small",
                transform=ax1.transAxes,
            )
            txt_std.set_bbox(
                {
                    "facecolor": "white",
                    "alpha": 0.95,
                    "edgecolor": "white",
                    "boxstyle": "square,pad=0",
                },
            )

        ax1.text(
            *plot_label_margin,
            panel_letter,
            ha=plot_label_ha,
            va=plot_label_va,
            fontsize=plot_label_size,
            fontweight=plot_label_weight,
            transform=ax1.transAxes,
        )

    #############################################################################
    # Plot: Histogram of differences from ICESat-2 mean (bathy only), if present.
    if has_bathy:
        nbins = 100  # fewer bins than land histogram
        ax2 = axes[panel_idx]
        panel_letter = panel_letters[panel_idx]
        panel_idx += 1

        ax2.hist(meandiff_bathy, bins=nbins, color="blue")
        # Unicode "minus" sign is \u2212
        ax2.set_title("DEM " + "\u2212" + " ICESat-2 elevation: bathy")
        ax2.set_ylabel("% of data cells")
        ax2.set_xlabel("Elevation difference (m)")
        ax2.yaxis.set_major_formatter(
            ticker.PercentFormatter(max(len(meandiff_bathy), 1), decimals=0),
        )

        # Add the lines for mean +- std
        center = np.mean(meandiff_bathy)
        std = np.std(meandiff_bathy)
        ax2.axvline(x=center, color="black", linewidth=0.75)
        ax2.axvline(x=center + std, color="black", linestyle="--", linewidth=0.5)
        ax2.axvline(x=center - std, color="black", linestyle="--", linewidth=0.5)

        # Crop the left & right (only if greater than 20 points)
        if len(meandiff_bathy) >= 20:
            cutoffs = np.percentile(meandiff_bathy, [1, 99])
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
        if np.any(np.isnan(cutoffs) | np.isinf(cutoffs)):
            cutoffs = [-1, 1]

        ax2.set_xlim(cutoffs)

        # Pad the top by 10%
        ylim = ax2.get_ylim()
        ax2.set_ylim((ylim[0], ylim[1] * 1.1))

        txt = ax2.text(
            0.11,
            0.95,
            rf"{center:.2f} $\pm$ {std:.2f} m",
            ha="left",
            va="top",
            fontsize="small",
            transform=ax2.transAxes,
        )
        txt.set_bbox(
            {
                "facecolor": "white",
                "alpha": 0.85,
                "edgecolor": "white",
                "boxstyle": "square,pad=0",
            },
        )

        # If requested, add the RMSE value to the figure.
        if also_add_rmse_to_hist:
            rmse = np.sqrt(np.mean(meandiff_bathy**2))
            txt_std = ax2.text(
                0.97,
                0.95,
                f"RMSE: {rmse:0.2f} m",
                ha="right",
                va="top",
                fontsize="small",
                transform=ax2.transAxes,
            )
            txt_std.set_bbox(
                {
                    "facecolor": "white",
                    "alpha": 0.95,
                    "edgecolor": "white",
                    "boxstyle": "square,pad=0",
                },
            )

        ax2.text(
            *plot_label_margin,
            panel_letter,
            ha=plot_label_ha,
            va=plot_label_va,
            fontsize=plot_label_size,
            fontweight=plot_label_weight,
            transform=ax2.transAxes,
        )

    # Plot: 1:1 line of DEM/ICESat-2 elevations (always present).
    #############################################################################
    ax3 = axes[panel_idx]
    panel_letter = panel_letters[panel_idx]

    dotsize = 3
    # Adjust the alpha depending how many points there are (more points == lighter dots)
    alpha = 0.35 * max(0.0025, min(4, (math.log10(100) / math.log10(len(mean_elev)))))
    # Can't have an alpha > 1.0, so cap it there.
    alpha = min(alpha, 1.0)

    # Scatter plots of both land and elev
    ax3.scatter(
        mean_elev[land_only_mask],
        dem_elev[land_only_mask],
        c="darkred",
        s=dotsize,
        linewidth=0,
        alpha=alpha,
    )
    ax3.scatter(
        mean_elev[~land_only_mask],
        dem_elev[~land_only_mask],
        c="blue",
        s=dotsize,
        linewidth=0,
        alpha=min(alpha * 2, 0.35),
    )
    ax3.set_title("DEM vs. ICESat-2")
    ax3.set_ylabel("DEM elevation (m)")
    ax3.set_xlabel("ICESat-2 elevation (m)")
    xlim = ax3.get_xlim()
    ylim = ax3.get_ylim()

    plotlim = (min(xlim[0], ylim[0]), max(xlim[1], ylim[1]))
    ax3.set_xlim(plotlim)
    ax3.set_ylim(plotlim)
    ax3.plot(plotlim, plotlim, ls="--", c=".3", lw=0.5, alpha=0.6)
    # Set the y-ticks the same as the x-ticks.
    xticks = ax3.get_xticks()
    ax3.set_yticks(xticks)

    ax3.text(
        *plot_label_margin,
        panel_letter,
        ha=plot_label_ha,
        va=plot_label_va,
        fontsize=plot_label_size,
        fontweight=plot_label_weight,
        transform=ax3.transAxes,
    )

    # Figure title
    if place_name is None:
        place_name = "DEM"

    rmse = (np.sum(meandiff**2) / len(meandiff)) ** 0.5

    fig.suptitle(
        f"{place_name}: Errors and Distributions\nRMSE = {rmse:0.3f} m,   N = {len(meandiff):,} cells",
    )
    fig.tight_layout()

    # Save the figure to disk.
    fig.savefig(output_figure_name)
    logger.debug("%s written.", output_figure_name)

    # Compute the RMSE and spit that out too.
    logger.info("\tRMSE: %s m", f"{rmse:0.3f}")

    # Clear the figure and close the plot.
    # If the plot is not "plt.close()"'ed, MatPlotLib keeps it in memory indefinitely
    # even after it's no longer referenced, which is... annoying. Gotta close it explicitly here.
    plt.clf()
    plt.close(fig)

    return
