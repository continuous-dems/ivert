import numpy
import re
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import pandas
import os
import math
from osgeo import gdal

####################################3
# Include the base /src/ directory of thie project, to add all the other modules.
import import_parent_dir; import_parent_dir.import_src_dir_via_pythonpath()
####################################3
import utils.configfile
my_config = utils.configfile.Config()
import icesat2.plot_validation_results

def add_lat_lons(df):
    """From the filename, get the lat/lon of each grid-cell point, from the filename and the i,j position of the pixel."""

    lat = numpy.empty((len(df),), dtype=float)
    lon = numpy.empty((len(df),), dtype=float)

    for idx, ((i,j), row) in enumerate(df.iterrows()):
        fname = row['filename']
        resolution_s = float(re.search('(?<=_)\d{1,2}(?=s_)', fname).group())
        res_deg = resolution_s / (60*60)
        tile_lat_str = re.search('(?<=_)[NS]\d{2}(?=[EW]\d{3}_)', fname).group()
        tile_lon_str = re.search('(?<=_[NS]\d{2})[EW]\d{3}(?=_)', fname).group()
        tile_lat = float(int(tile_lat_str[1:]) * (1 if tile_lat_str[0] == "N" else -1))
        tile_lon = float(int(tile_lon_str[1:]) * (1 if tile_lon_str[0] == "E" else -1))

        lat[idx] = tile_lat - (res_deg * i)
        lon[idx] = tile_lon + (res_deg * j)

    df['latitude'] = lat
    df['longitude'] = lon

    return df

def get_slopes(df, files_dirname):
    fnames = [os.path.join(files_dirname, fn) for fn in df.filename.unique()]
    slope_fnames = [fn.replace("_results.h5", "_slope.tif") for fn in fnames]
    fnames_dict = dict([(os.path.basename(fn), sfn) for fn, sfn in zip(fnames, slope_fnames)])

    fn_array_dict = {}
    slopes = numpy.empty((len(df),), dtype=float)
    for idx,((i,j), row) in enumerate(df.iterrows()):
        fname = row.filename
        if fname in fn_array_dict:
            array = fn_array_dict[fname]
        else:
            fname_slope = fnames_dict[fname]
            if not os.path.exists(fname_slope):
                raise FileNotFoundError(fname_slope)
            ds = gdal.Open(fname_slope, gdal.GA_ReadOnly)
            array = ds.GetRasterBand(1).ReadAsArray()
            ds = None
            fn_array_dict[fname] = array

        slopes[idx] = array[i,j]

    df['slope'] = slopes

    return df

def plot_errors_against_slope_centrality(results_h5_or_list,
                        output_figure_name,
                        empty_val = my_config.etopo_ndv,
                        coverage_cutoff_pct = 40,
                        fig_title = None,
                        dpi=600,
                        exclude_ice_sheets = False,
                        verbose=True):
    total_results_h5 = os.path.join(os.path.dirname(output_figure_name), "total_results.h5")
    if os.path.exists(total_results_h5):
        data = pandas.read_hdf(total_results_h5)
        if verbose:
            print(os.path.basename(total_results_h5), "read.")
    else:

        data = icesat2.plot_validation_results.get_data_from_h5_or_list(results_h5_or_list,
                                                                        empty_val = empty_val,
                                                                        include_filenames=True,
                                                                        verbose=verbose)
        # dirname = os.path.dirname(results_h5_or_list[0])

        # get_slopes(data, dirname)

        if exclude_ice_sheets:
            add_lat_lons(data)
            lat = data.latitude
            lon = data.longitude
            # Get only data that is NOT in Antarctica or Greenland.
            data = data[(lat >= -67.0) &
                        ~(((lat >= 60) & (lat < 68) & (lon > -55) & (lon < -30)) |
                          ((lat >= 68) & (lon > -75) & (lon < -12)))]


        data.to_hdf(total_results_h5, "results_all")

        if verbose:
            print(os.path.basename(total_results_h5), "written.")

    print(data.columns)
    print(data)

    meandiff        = data['diff_mean']
    # numphotons      = data['numphotons']
    # numphotons_intd = data['numphotons_intd']
    # canopy_fraction = data["canopy_fraction"]
    # dem_elev        = data["dem_elev"]
    # mean_elev       = data["mean"]
    # dist_from_center= data["min_dist_from_center"]
    coverage_frac   = data["coverage_frac"]
    # slope           = data["slope"]


    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2,2, dpi=dpi, figsize=(8, 5))
    low, hi = numpy.percentile(meandiff, [2.5,97.5])
    ax1.hist(meandiff, bins=100, range=(low, hi))
    ax1.set_ylabel("% of data cells")
    ax1.set_xlabel("Elevation difference (m)")
    ax1.yaxis.set_major_formatter(ticker.PercentFormatter(len(meandiff), decimals=0))

    # Add the lines for mean +- std
    center = numpy.mean(meandiff)
    std = numpy.std(meandiff)
    ax1.axvline(x=center, color="darkgreen", linewidth=0.75)
    ax1.axvline(x=center+std, color="darkgreen", linestyle="--", linewidth=0.5)
    ax1.axvline(x=center-std, color="darkgreen", linestyle="--", linewidth=0.5)

    # Detect whether the mean line is closer to the left or the right (we'll put the text box on the other side)
    text_left = not ((center - low) < (hi - center))
    txt = ax1.text(0.12 if text_left else 0.97,
                   0.95, # 0.85 if text_left else 0.95,
                   "{0:.2f} $\pm$ {1:.2f} m".format(center, std),
                   ha="left" if text_left else "right",
                   va="top",
                   fontsize="small",
                   transform=ax1.transAxes)
    txt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))

    coverage_pct = coverage_frac * 100
    alpha = 0.5 * max(0.0025, min(4, (math.log10(100)/math.log10(len(meandiff)))))
    ax2.scatter(coverage_pct, meandiff, marker='o', lw=0, s=0.5, alpha=alpha)
    ax2.set_xlabel("Cell coverage (%)")
    ax2.set_ylabel("Error (m)")

    # There definitely seems to be a relationship between errors and % coverage of each pixel. Plot it over this.
    unique_coverages = numpy.unique(coverage_pct)
    coverage_lo = numpy.zeros((len(unique_coverages),))
    coverage_hi = numpy.zeros((len(unique_coverages),))
    coverage_rmse = numpy.zeros(len(unique_coverages,))
    coverage_counts = numpy.zeros(len(unique_coverages,))
    for i,c in enumerate(unique_coverages):
        cmask = (coverage_pct == c)
        diff_subset = meandiff[cmask]
        coverage_lo[i], coverage_hi[i] = numpy.percentile(diff_subset, (2,98))
        coverage_rmse[i] = numpy.sqrt(numpy.mean(diff_subset ** 2))
        coverage_counts[i] = diff_subset.size

    ax2.fill_between(unique_coverages, coverage_lo, coverage_hi, alpha=0.3, color="maroon")

    # Figure 3, plot against slope
    # ax3.scatter(slope, meandiff, marker="o", lw=0, s=0.5, alpha=alpha)
    # ax3.set_xlabel("Slope ($^\circ$)")
    # ax3.set_ylabel("Error (m)")

    # Figure 3, RMSE vs coverage:

    # Plot the distribution of coverages at each level.
    ax3_2 = ax3.twinx()
    ax3.bar(unique_coverages, coverage_counts, width=0.5, alpha=0.8, color="C0")
    ax3.yaxis.set_major_formatter(ticker.PercentFormatter(len(meandiff), decimals=0))
    ax3.set_ylabel("% of data cells", color="C0")
    ax3.set_xlabel("ICESat-2 cell coverage (%)")
    # Then plot the coverage RMSEs
    ax3_2.plot(unique_coverages, coverage_rmse, color="darkgreen")
    ax3_2.set_ylabel("RMSE (m)", color="darkgreen")

    # Fig 4: Plot only the points with "good" coverage.
    good_coverage_mask = coverage_pct >= coverage_cutoff_pct
    meandiff_good = meandiff[good_coverage_mask]
    # coverage_good = coverage_pct[good_coverage_mask]

    low, hi = numpy.percentile(meandiff, [5,90])
    ax4.hist(meandiff_good, bins=100, range=(low, hi), color="darkred", alpha=0.6)
    ax4.set_ylabel("% of data cells")
    ax4.set_xlabel("Elevation difference (m)")
    ax4.yaxis.set_major_formatter(ticker.PercentFormatter(len(meandiff_good), decimals=0))

    # Add the lines for mean +- std
    center = numpy.mean(meandiff_good)
    std = numpy.std(meandiff_good)
    ax4.axvline(x=center, color="darkred", linewidth=0.75)
    ax4.axvline(x=center+std, color="darkred", linestyle="--", linewidth=0.5)
    ax4.axvline(x=center-std, color="darkred", linestyle="--", linewidth=0.5)

    # Detect whether the mean line is closer to the left or the right (we'll put the text box on the other side)
    text_left = not ((center - low) < (hi - center))

    txt = ax4.text(0.03, 0.95,
                   "Only data with\n$\geq${0} % coverage".format(coverage_cutoff_pct),
                   ha="left", va="top",
                   fontsize="small",
                   transform=ax4.transAxes)
    txt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))

    txt = ax4.text(0.03 if text_left else 0.97,
                   0.70,
                   "{0:.2f} $\pm$ {1:.2f} m".format(center, std),
                   ha="left" if text_left else "right",
                   va="top",
                   fontsize="small",
                   transform=ax4.transAxes)
    txt.set_bbox(dict(facecolor="white", alpha=0.7, edgecolor="white", boxstyle="square,pad=0"))


    fig.tight_layout()

    if fig_title:
        fig.suptitle(fig_title)

    fig.savefig(output_figure_name, dpi=dpi)
    if verbose:
        print(output_figure_name, "written.")

    print(len(meandiff), "original cells.")
    print(len(meandiff_good), "subset cells.")

if __name__ == "__main__":
    dirname = "/home/mmacferrin/Research/DATA/ETOPO/data/validation_results/15s/2022.09.29"
    h5_list = sorted([os.path.join(dirname, fn) for fn in os.listdir(dirname) if re.search("_results.h5", fn) is not None])
    outdir = os.path.join(dirname, "plots")

    # print("reading", h5_list[0])
    plot_errors_against_slope_centrality(h5_list,
                                         os.path.join(outdir, "coverage_results.png"),
                                         coverage_cutoff_pct=47,
                                         exclude_ice_sheets=True) #, fig_title=os.path.basename(h5_list[0]))
