"""Plot histograms and error statistics from IVERT validation results."""

import collections
import logging
import math
from pathlib import Path
from typing import NamedTuple

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tqdm
from matplotlib import ticker
from matplotlib.axes import Axes

from ivert.utils import dem_source

logger = logging.getLogger(__name__)

# Fewer validated cells than this are too few to plot statistics for.
_MIN_CELLS_TO_PLOT = 3
# A histogram of at least this many cells is cropped to its 1st-99th percentiles.
_MIN_CELLS_TO_CROP = 20


def is_iterable(obj: object) -> bool:
    """Tell whether an object is a non-string iterable. (list, tuple, etc)."""
    return isinstance(obj, collections.abc.Iterable) and not isinstance(
        obj,
        str,
    )


def get_data_from_h5_or_list(
    h5_name_or_list: str | Path | list[str | Path],
    orig_filenames: str | list[str] | None = None,
    *,
    include_filenames: bool = False,
) -> pd.DataFrame:
    """Return the data from one HDF5 results file, or from a list of them."""
    if isinstance(h5_name_or_list, (str, Path)):
        if orig_filenames is not None and not isinstance(orig_filenames, str):
            msg = "orig_filenames must be a string when h5_name_or_list is one."
            raise TypeError(msg)
        data = _read_results_file(
            h5_name_or_list,
            orig_filenames,
            include_filenames=include_filenames,
        )

    elif is_iterable(h5_name_or_list):
        logger.info("Reading %s h5 results files.", len(h5_name_or_list))
        if orig_filenames is not None and not is_iterable(orig_filenames):
            msg = "orig_filenames must be a list when h5_name_or_list is one."
            raise TypeError(msg)
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
                data_list.append(
                    _read_results_file(
                        h5_file,
                        None if orig_filenames is None else orig_filenames[i],
                        include_filenames=include_filenames,
                    ),
                )

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


def _read_results_file(
    h5_file: str | Path,
    orig_filename: str | None,
    *,
    include_filenames: bool,
) -> pd.DataFrame:
    """Read one HDF5 results file, adding a 'filename' column if asked to.

    The column holds the display name of 'orig_filename', the DEM the results came
    from, or the results file's own name when that isn't given.
    """
    data = pd.read_hdf(h5_file)
    if include_filenames:
        if orig_filename is None:
            data["filename"] = Path(h5_file).name
        else:
            data["filename"] = dem_source.dem_display_name(orig_filename)
    return data


class _HistogramStyle(NamedTuple):
    """How one histogram panel is drawn: its bins, colour and surface name."""

    nbins: int
    color: str
    surface: str


_LAND_HISTOGRAM = _HistogramStyle(nbins=200, color="darkred", surface="land")
# Fewer bins than the land histogram.
_BATHY_HISTOGRAM = _HistogramStyle(nbins=100, color="blue", surface="bathy")


def plot_histograms_and_line(
    results_h5_or_list_or_df: pd.DataFrame | str | Path | list[str | Path],
    output_figure_name: str | Path,
    *,
    place_name: str | None = None,
    figsize: tuple[float, float] = (10.0, 4.0),  # Width/height, in inches
    labels_uppercase: bool = True,
    dpi: int = 600,
    hist_cutoff_num_stddevs: float = 2.5,
    also_add_rmse_to_hist: bool = False,
) -> None:
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

    if isinstance(results_h5_or_list_or_df, pd.DataFrame):
        data = results_h5_or_list_or_df
    else:
        data = get_data_from_h5_or_list(results_h5_or_list_or_df)

    meandiff = data["diff_mean"]
    numphotons_bathy = data["numphotons_bathy"]
    dem_elev = data["dem_elev"]
    mean_elev = data["mean"]

    if len(meandiff) < _MIN_CELLS_TO_PLOT:
        logger.info("Not enough cells to plot statistics. Aborting.")
        return

    # Determine which histogram panels have data before creating the figure.
    land_only_mask = numphotons_bathy == 0
    meandiff_land = meandiff[land_only_mask]
    bathy_mask = numphotons_bathy > 0
    meandiff_bathy = meandiff[bathy_mask]
    histograms = [
        (diffs, style)
        for diffs, style in (
            (meandiff_land, _LAND_HISTOGRAM),
            (meandiff_bathy, _BATHY_HISTOGRAM),
        )
        if len(diffs) > 0
    ]

    # ncols = number of histogram panels present + 1 scatter panel.
    ncols = len(histograms) + 1

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

    panel_letters = "ABCDE" if labels_uppercase else "abcde"

    # Histograms of differences from the ICESat-2 mean, land then bathy, if present.
    for panel_idx, (diffs, style) in enumerate(histograms):
        _plot_error_histogram(
            axes[panel_idx],
            diffs,
            style,
            panel_letter=panel_letters[panel_idx],
            hist_cutoff_num_stddevs=hist_cutoff_num_stddevs,
            also_add_rmse_to_hist=also_add_rmse_to_hist,
        )

    # 1:1 line of DEM/ICESat-2 elevations (always present).
    _plot_elevation_scatter(
        axes[len(histograms)],
        mean_elev,
        dem_elev,
        land_only_mask,
        panel_letter=panel_letters[len(histograms)],
    )

    # Figure title
    if place_name is None:
        place_name = "DEM"

    rmse = (np.sum(meandiff**2) / len(meandiff)) ** 0.5

    fig.suptitle(
        f"{place_name}: Errors and Distributions\n"
        f"RMSE = {rmse:0.3f} m,   N = {len(meandiff):,} cells",
    )
    fig.tight_layout()

    # Save the figure to disk.
    fig.savefig(output_figure_name)
    logger.debug("%s written.", output_figure_name)

    # Compute the RMSE and spit that out too.
    logger.info("\tRMSE: %s m", f"{rmse:0.3f}")

    # Clear the figure and close the plot.
    # If the plot is not "plt.close()"'ed, MatPlotLib keeps it in memory indefinitely
    # even after it's no longer referenced, which is... annoying. Gotta close it
    # explicitly here.
    plt.clf()
    plt.close(fig)

    return


def _plot_error_histogram(
    ax: Axes,
    diffs: pd.Series,
    style: _HistogramStyle,
    *,
    panel_letter: str,
    hist_cutoff_num_stddevs: float,
    also_add_rmse_to_hist: bool,
) -> None:
    """Draw a histogram of DEM minus ICESat-2 differences, with its mean and std."""
    ax.hist(diffs, bins=style.nbins, color=style.color)
    # Unicode "minus" sign is \u2212
    ax.set_title("DEM " + "\u2212" + " ICESat-2 elevation: " + style.surface)
    ax.set_ylabel("% of data cells")
    ax.set_xlabel("Elevation difference (m)")
    ax.yaxis.set_major_formatter(
        ticker.PercentFormatter(max(len(diffs), 1), decimals=0),
    )

    # Add the lines for mean +- std
    center = np.mean(diffs)
    std = np.std(diffs)
    ax.axvline(x=center, color="black", linewidth=0.75)
    ax.axvline(x=center + std, color="black", linestyle="--", linewidth=0.5)
    ax.axvline(x=center - std, color="black", linestyle="--", linewidth=0.5)

    ax.set_xlim(_histogram_xlim(diffs, center, std, hist_cutoff_num_stddevs))

    # Pad the top by 10%
    ylim = ax.get_ylim()
    ax.set_ylim((ylim[0], ylim[1] * 1.1))

    _add_text_box(ax, 0.11, rf"{center:.2f} $\pm$ {std:.2f} m", ha="left", alpha=0.85)

    # If requested, add the RMSE value to the figure.
    if also_add_rmse_to_hist:
        rmse = np.sqrt(np.mean(diffs**2))
        _add_text_box(ax, 0.97, f"RMSE: {rmse:0.2f} m", ha="right", alpha=0.95)

    _label_panel(ax, panel_letter)


def _histogram_xlim(
    diffs: pd.Series,
    center: float,
    std: float,
    hist_cutoff_num_stddevs: float,
) -> tuple[float, float]:
    """Return the (left, right) x-axis limits for a histogram of differences."""
    # Crop the left & right (only if there are enough points)
    if len(diffs) >= _MIN_CELLS_TO_CROP:
        cutoffs = np.percentile(diffs, [1, 99])
    else:
        cutoffs = [min(diffs), max(diffs)]

    # If we have a zero-width range, arbitrarily buffer it by 1 m in each direction.
    if cutoffs[0] == cutoffs[1]:
        cutoffs[0] = cutoffs[0] - 1
        cutoffs[1] = cutoffs[1] + 1

    # Do not crop the photo to make the stddev lines fall outside the plot.
    # If they do, reset the min/max cutoff to be 2 stddev away from the mean on that
    # side.
    if (center + std) >= cutoffs[1] or hist_cutoff_num_stddevs is not None:
        cutoffs[1] = center + (std * hist_cutoff_num_stddevs)
    if (center - std) <= cutoffs[0] or hist_cutoff_num_stddevs is not None:
        cutoffs[0] = center - (std * hist_cutoff_num_stddevs)

    # Just error checking, if any of the cutoffs come back with NaN or Inf, just
    # clip it to -1, 1, debug later.
    if np.any(np.isnan(cutoffs) | np.isinf(cutoffs)):
        cutoffs = [-1, 1]

    return (float(cutoffs[0]), float(cutoffs[1]))


def _add_text_box(ax: Axes, x: float, text: str, *, ha: str, alpha: float) -> None:
    """Write small text near the top of a panel, on a white box."""
    txt = ax.text(
        x,
        0.95,
        text,
        ha=ha,
        va="top",
        fontsize="small",
        transform=ax.transAxes,
    )
    txt.set_bbox(
        {
            "facecolor": "white",
            "alpha": alpha,
            "edgecolor": "white",
            "boxstyle": "square,pad=0",
        },
    )


def _plot_elevation_scatter(
    ax: Axes,
    mean_elev: pd.Series,
    dem_elev: pd.Series,
    land_only_mask: pd.Series,
    *,
    panel_letter: str,
) -> None:
    """Plot DEM against ICESat-2 elevations, land and bathy cells, with a 1:1 line."""
    dotsize = 3
    # Adjust the alpha depending how many points there are (more points == lighter dots)
    alpha = 0.35 * max(0.0025, min(4, (math.log10(100) / math.log10(len(mean_elev)))))
    # Can't have an alpha > 1.0, so cap it there.
    alpha = min(alpha, 1.0)

    # Scatter plots of both land and elev
    ax.scatter(
        mean_elev[land_only_mask],
        dem_elev[land_only_mask],
        c="darkred",
        s=dotsize,
        linewidth=0,
        alpha=alpha,
    )
    ax.scatter(
        mean_elev[~land_only_mask],
        dem_elev[~land_only_mask],
        c="blue",
        s=dotsize,
        linewidth=0,
        alpha=min(alpha * 2, 0.35),
    )
    ax.set_title("DEM vs. ICESat-2")
    ax.set_ylabel("DEM elevation (m)")
    ax.set_xlabel("ICESat-2 elevation (m)")
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()

    plotlim = (min(xlim[0], ylim[0]), max(xlim[1], ylim[1]))
    ax.set_xlim(plotlim)
    ax.set_ylim(plotlim)
    ax.plot(plotlim, plotlim, ls="--", c=".3", lw=0.5, alpha=0.6)
    # Set the y-ticks the same as the x-ticks.
    xticks = ax.get_xticks()
    ax.set_yticks(xticks)

    _label_panel(ax, panel_letter)


def _label_panel(ax: Axes, letter: str) -> None:
    """Write a panel's letter in its top-left corner."""
    ax.text(
        0.015,
        0.97,
        letter,
        ha="left",
        va="top",
        fontsize="large",
        fontweight="book",
        transform=ax.transAxes,
    )
