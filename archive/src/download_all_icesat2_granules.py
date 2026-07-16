# -*- coding: utf-8 -*-

import os
import numpy
import math
import argparse
import multiprocessing
import time
import re
import shutil
import pandas
import geopandas
import datetime
import tables.exceptions
import shapely.geometry
import matplotlib.pyplot as plt
import cartopy.crs

#####################################
# Suppress the annoying pandas.FutureWarning warnings caused by library version conflicts.
# It doesn't affect my code and will be resolved in future releases of pandas.
# For now, just suppress the warning.
import warnings

warnings.filterwarnings("ignore", message=".*pandas.Int64Index is deprecated*")
#####################################

####################################3
# Include the base /src/ directory of thie project, to add all the other modules.
import import_parent_dir

import_parent_dir.import_src_dir_via_pythonpath()
####################################3
import utils.configfile

my_config = utils.configfile.Config()
import icesat2.nsidc_download as nsidc_download
import icesat2.classify_icesat2_photons as classify_icesat2_photons
import icesat2.icesat2_photon_database as icesat2_photon_database
import etopo.generate_empty_grids
import utils.progress_bar
import utils.sizeof_format
import utils.parallel_funcs

# ICESAT2_DIR = os.path.join(my_config.etopo_cudem_cache_directory, ".cudem_cache")
ICESAT2_DIR = my_config.icesat2_granules_directory


def move_all_granules_to_icesat2_granules_dir(
    old_dir=os.path.join(my_config.etopo_cudem_cache_directory, ".cudem_cache"),
    new_dir=ICESAT2_DIR,
    h5_regex="ATL(\w+)\.h5\Z",
):
    """I'd previously been putting all the ICESAT-2 granules in /scratch_data/.cudem_cache with the
    waffles cached files. I decided I want them in the data/icesat2/granules directory instead. So
    I'm gonna move all the existing icesat-2 files over there."""
    icesat2_files = [
        fn for fn in os.listdir(old_dir) if re.search(h5_regex, fn) != None
    ]

    if len(icesat2_files) == 0:
        print("No existing icesat-2 files in", old_dir + ". Exiting.")
        return

    print("Moving", len(icesat2_files), "files to new ICESat-2 directory.")
    for i, fname in enumerate(icesat2_files):
        old = os.path.join(old_dir, fname)
        new = os.path.join(new_dir, fname)
        shutil.move(old, new)
        utils.progress_bar.ProgressBar(
            i + 1,
            len(icesat2_files),
            suffix="{0}/{1}".format(i + 1, len(icesat2_files)),
        )


def create_query_bounding_boxes(xmin=-180, xmax=180, xres=1, ymin=-89, ymax=89, yres=1):
    """We can't query & download the whole entire world instantly. So, let's
    generate a series of 5x5-degree bounding boxes in which to query for data.
    """
    bboxes = []

    assert xmin < xmax
    assert ymin < ymax

    # Go either direction from the equator. Get to the polar regions last.
    if ymax > 0 and ymin < 0:
        y_vals = list(zip(range(0, ymax, yres), range(-1, ymin, -1)))
        y_vals = numpy.array(y_vals).flatten()
    else:
        y_vals = range(ymin, ymax, yres)

    for y in y_vals:
        for x in range(xmin, xmax, xres):
            bbox = (x, y, x + 1, y + 1)
            bbox = tuple([float(n) for n in bbox])
            bboxes.append(bbox)

    return bboxes


def create_icesat2_progress_csv(
    fname=my_config.icesat2_download_progress_csv, from_gpkg=False, verbose=True
):
    """Create a CSV file so that we can keep track of which icesat-2 files
    have been downloaded already and which ones have not. This is much more efficient than
    contantly querying NSIDC again and again to see if we have the files for every
    given 1x1-deg tile over the planet.

    If from_gpkg, then build it from the geopackage rather than from scratch. This is useful
    if we accidentally corrupted the CSV file while downloading.
    """
    if from_gpkg:
        gpkg_fname = os.path.splitext(fname)[0] + ".gpkg"
        if verbose:
            print("Reading", os.path.split(gpkg_fname)[1] + "...")
        gdf = geopandas.read_file(gpkg_fname)
        df = pandas.DataFrame(gdf.drop(columns="geometry"))

    else:
        if verbose:
            print("Fetching all ICESat-2 1x1-deg bounding boxes. This can take a sec.")
        tile_tuples = etopo.generate_empty_grids.create_list_of_tile_tuples(
            resolution=1, verbose=False
        )
        bboxes = [
            (int(x), int(y), int(x + 1), int(y + 1))
            for (y, x) in tile_tuples
            if (y > -90)
        ]
        df = pandas.DataFrame(
            data={
                "xmin": [bb[0] for bb in bboxes],
                "ymin": [bb[1] for bb in bboxes],
                "xmax": [bb[2] for bb in bboxes],
                "ymax": [bb[3] for bb in bboxes],
                "numgranules": numpy.zeros((len(bboxes),), dtype=int),
                "is_downloaded": numpy.zeros((len(bboxes),), dtype=bool),
            }
        )

        if verbose:
            print(len(bboxes), "bounding boxes total.")

    df.to_csv(fname, index=False)
    if verbose:
        print(fname, "written.")

    return


def create_download_progress_map(
    download_gdf,
    map_filename,
    progress_fieldname="is_downloaded",
    map_title="ETOPO NSIDC Download Progress",
    cmap="jet",
    alpha=0.6,
    verbose=True,
):
    """Generate an image of a world map showing areas that have been downloaded."""
    world = geopandas.read_file(geopandas.datasets.get_path("naturalearth_lowres"))
    fig = plt.figure(figsize=(12, 7))
    prj = cartopy.crs.PlateCarree()
    ax = plt.axes(projection=prj)
    # fig, ax = plt.subplots(1,1, figsize=(12,7)) #, projection=4326)
    # ax.set_xlim(-180, 180)
    # ax.set_ylim(-90, 90)
    ax.set_extent([-180, 180, -90, 90], crs=prj)
    # Add the background world in light grey
    world.plot(color="lightgrey", ax=ax)
    # Add lat/lon graticules every 10 degrees, with labels
    ax.gridlines(
        xlocs=numpy.arange(-180, 180, 10),
        ylocs=numpy.arange(-90, 90, 10),
        draw_labels=True,
        x_inline=False,
        y_inline=False,
        color="k",
        linestyle="dotted",
        linewidth=0.5,
    )
    # Plot the boxes, colored by their downloaded status
    # dmin = min(download_gdf[progress_fieldname])
    dmax = max(download_gdf[progress_fieldname])
    cm_obj = plt.get_cmap(cmap, lut=2)
    # color_dmin = tuple(list(cm_obj(dmin)[0:3]) + [alpha])
    color_dmax = tuple(list(cm_obj(dmax)[0:3]) + [alpha])

    # If "is_downloaded" or "is_populated" field is all true, create an empty row at the end with a non-existent polygon.
    # Just need to give at least one "false" reading in order for the colormap to
    # still show correctly on a fully-completed map where 'is_downloaded' are all True.
    if numpy.all(download_gdf[progress_fieldname]):
        added_row = True
        if progress_fieldname == "is_downloaded":
            download_gdf.loc[len(download_gdf)] = [
                0,
                0,
                0,
                0,
                0,
                False,
                shapely.geometry.Polygon(([0, 0], [0.00001, 0], [0, 0])),
            ]
        elif progress_fieldname == "is_populated":
            download_gdf.loc[len(download_gdf)] = [
                "",
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                False,
                shapely.geometry.Polygon(([0, 0], [0.00001, 0], [0, 0])),
            ]
    else:
        added_row = False

    # Add the data to the plot.
    download_gdf.plot(column=progress_fieldname, ax=ax, alpha=alpha, cmap=cm_obj)

    # Get rid of that temporary extra row if we added it.
    if added_row:
        # After it's plotted, drop the extra row we just added.
        download_gdf.drop(download_gdf.tail(1).index, inplace=True)

    # Add some helpful text to the figure.
    plt.text(
        0.5,
        0.9,
        map_title,
        ha="center",
        va="center",
        fontsize="x-large",
        fontweight="bold",
        transform=fig.transFigure,
    )

    total_tiles = len(download_gdf)
    tiles_downloaded = numpy.count_nonzero(download_gdf[progress_fieldname])
    pct_download = 100.0 * tiles_downloaded / total_tiles

    # Key for the complete color
    plt.text(
        0.13,
        0.81,
        "■ = complete",
        ha="left",
        va="center",
        color=color_dmax,
        backgroundcolor="white",
        fontsize="medium",
        fontweight="bold",
        bbox=dict(boxstyle="square,pad=0.1", fc="white", ec="none"),
        transform=fig.transFigure,
    )

    plt.text(
        0.5,
        0.10,
        "{0:,} of {1:,} tiles complete, {2:.1f}%".format(
            tiles_downloaded, total_tiles, pct_download
        ),
        ha="center",
        va="center",
        fontsize="large",
        fontweight="bold",
        transform=fig.transFigure,
    )
    now_str = datetime.datetime.now().astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")
    plt.text(
        0.5,
        0.06,
        "Last updated " + now_str,
        ha="center",
        va="center",
        fontsize="large",
        transform=fig.transFigure,
    )

    # Save the figure.
    fig.savefig(map_filename, dpi=300)
    if verbose:
        print(os.path.split(map_filename)[1], "written.")
    # Make sure to close the figure to free up the memory.
    plt.close(fig)

    return


def subset_existing_photon_databases_to_ground_and_canopy_only():
    # THIS FUNCTION IS NO LONGER NEEDED. DO NOT RUN AGAIN.
    """The original version of these database has all the photons included.
    Read through them all, eliminate all that aren't ground (1), canopy (2), or canopy_top(3).
    This should help reduce file sizes some."""

    # NOTE: SHould only need to run this function once! Do not bother running again!
    # I've since updated the classify_icesat2_photons.save_granule_ground_photons() to only save ground and canopy photons rather than all of them.
    # Anything saved later than the "datetime_cutoff" should have this filtering already included and not need to be subset.
    print("Surveying ICESat-2 directory. Hold on a sec...")
    db_files = [
        os.path.join(ICESAT2_DIR, fn)
        for fn in os.listdir(ICESAT2_DIR)
        if (re.search("_photons\.h5\Z", fn) != None)
    ]

    # Any files that have been last written past this date likely do not need to be reduced.
    # I'd started subsetting the files before saving after this.
    datetime_cutoff = datetime.datetime(2022, 5, 21, 12, 0, 0)

    print(len(db_files), "photon database files found.")
    for i, db in enumerate(db_files):
        try:
            # Check to see if it's older than when we made the changes to how new databases were subset.
            # If it's a newer file, it would alreaady be subset, so skip this step.
            if datetime.datetime.fromtimestamp(os.path.getmtime(db)) <= datetime_cutoff:
                print("\r" + " " * 120 + "\r", end="")
                print("{0}/{1}".format(i + 1, len(db_files)), end=" ")
                # print(os.path.split(db)[1], "is newer than cutoff date..")
                print(
                    os.path.split(db)[1],
                    "is older than cutoff date and has already been done..",
                )

            else:
                fsize_orig = os.path.getsize(db)
                df = pandas.read_hdf(db, mode="r")

                # Get only the photons whose "class_code" is 1 to 3
                subset_mask = df.class_code.between(1, 3, inclusive="both")
                if numpy.all(subset_mask):
                    print("\r" + " " * 120 + "\r", end="")
                    print("{0}/{1}".format(i + 1, len(db_files)), end=" ")
                    print(os.path.split(db)[1], "not reduced.")
                else:
                    df_subset = df.loc[subset_mask]
                    df_subset.to_hdf(
                        db, "icesat2", complib="zlib", complevel=3, mode="w"
                    )
                    fsize_new = os.path.getsize(db)
                    reduction_pct = (fsize_orig - fsize_new) / fsize_orig * 100
                    print("\r" + " " * 120 + "\r", end="")
                    print("{0}/{1}".format(i + 1, len(db_files)), end=" ")
                    print(
                        os.path.split(db)[1],
                        utils.sizeof_format.sizeof_fmt(fsize_orig),
                        "->",
                        utils.sizeof_format.sizeof_fmt(fsize_new) + ",",
                        "{0:.1f}% reduction".format(reduction_pct),
                    )
        except (AttributeError, tables.exceptions.HDF5ExtError):
            # If we can't find the module in the file that we're looking for, or
            # if an HDF5ExtError popped up, it
            # was probably incorrectly or incompletely saved. Delete it.
            os.remove(db)
            print("\r" + " " * 120 + "\r", end="")
            print("{0}/{1}".format(i + 1, len(db_files)), end=" ")
            print(os.path.split(db)[1], "has an error. Deleting.")

        utils.progress_bar.ProgressBar(
            i + 1, len(db_files), suffix="{0}/{1}".format(i + 1, len(db_files))
        )


def create_tiling_progress_map(
    icesat2_db=None, update_with_csvs=True, use_tempfile=True, verbose=True
):
    if icesat2_db is None:
        icesat2_db = icesat2_photon_database.ICESat2_Database()

    gdf = icesat2_db.get_gdf(verbose=verbose)

    if update_with_csvs:
        gdf = icesat2_db.update_gpkg_with_csvfiles(
            gdf=gdf, use_tempfile=use_tempfile, verbose=verbose
        )

    map_fname = icesat2_db.get_tiling_progress_mapname()
    create_download_progress_map(
        gdf,
        map_fname,
        progress_fieldname="is_populated",
        map_title="ETOPO Photon Tile Database Progress",
        verbose=verbose,
    )


def delete_granules_where_photon_databases_exist():
    """Get rid of granules in which we've already created a _photon.h5 database.

    We don't need the original granules anymore.
    """
    print("Surveying ICESat-2 directory. Hold on a sec...")
    db_files = [
        os.path.join(ICESAT2_DIR, fn)
        for fn in os.listdir(ICESAT2_DIR)
        if (re.search("_photons\.h5\Z", fn) != None)
    ]

    files_deleted = 0

    print(len(db_files), "photon database files found.")
    for i, db in enumerate(db_files):
        atl03_fname = db.replace("_photons.h5", ".h5")
        atl08_fname = atl03_fname.replace("ATL03", "ATL08", 1)
        if os.path.exists(atl03_fname):
            os.remove(atl03_fname)
            files_deleted += 1
        if os.path.exists(atl08_fname):
            os.remove(atl08_fname)
            files_deleted += 1
        utils.progress_bar.ProgressBar(
            i + 1, len(db_files), suffix="{0}/{1}".format(i + 1, len(db_files))
        )

    print(files_deleted, "files deleted.")


def download_all(
    overwrite=False,
    track_progress=True,
    update_map_every_N=10,
    retry_count=5,
    verbose=True,
):
    """Square by square, go through and download the whole fuckin' world of ICESat-2 photons."""
    tile_tuples = etopo.generate_empty_grids.create_list_of_tile_tuples(
        resolution=1, verbose=False
    )

    # Get rid of bottom and top latitudes here, since icesat-2 has a 1.2* pole-hole.
    tile_array = numpy.array(
        [tt for tt in tile_tuples if tt[0] not in (-90, 89)],
        dtype=numpy.dtype([("y", numpy.int16), ("x", numpy.int16)]),
    )
    # put in order from zero-latitude northward & southward... do the poles last.
    tile_array_to_argsort = tile_array.copy()
    tile_array_to_argsort["y"] = numpy.abs(tile_array_to_argsort["y"])
    sort_mask = numpy.argsort(tile_array_to_argsort, order=("y", "x"))
    tile_array_sorted = tile_array[sort_mask]

    bboxes = [(int(x), int(y), int(x + 1), int(y + 1)) for (y, x) in tile_array_sorted]

    # Use the download_progress.csv file to track which tiles have been downloaded and which have not yet.
    if track_progress:
        progress_fname = my_config.icesat2_download_progress_csv
        progress_df = pandas.read_csv(progress_fname)

        # For some reason a number of "Unnamed" columns are being added. Delete them if they're in there.
        cols_with_unnamed_in_them = [
            colname
            for colname in progress_df.columns
            if colname.lower().find("unnamed") > -1
        ]
        progress_df = progress_df.drop(cols_with_unnamed_in_them, axis=1)

        if len(progress_df) == 0:
            # Something got fucked up, rebuild the progress_df
            progress_df = rebuild_progress_csv_from_gpkg(progress_fname)

        # Also, it appears the progress csv has the last-degree polar tiles in them. Delete those.
        if len(progress_df) > 26110:
            orig_length = len(progress_df)
            progress_df = progress_df[progress_df["ymin"] != -90].copy()
            if verbose:
                print(
                    "Trimmed last-degree polar tiles from {0} to {1}.".format(
                        orig_length, len(progress_df)
                    )
                )
            progress_df.to_csv(progress_fname)
            if verbose:
                print(os.path.split(progress_fname)[1], "re-written.")

        if verbose:
            print(progress_fname, "read.")

    # ################### DELETE THESE ###########################################
    # # Just code thrown in here to create the map at the very end. Un-comment all this if you want to re-create the final download map.
    # gpkg_fname = os.path.splitext(progress_fname)[0] + ".gpkg"
    # progress_gdf = output_progress_csv_to_gpkg(progress_df, gpkg_fname, verbose=verbose)

    # map_filename = os.path.splitext(gpkg_fname)[0] + "_map.png"
    # create_download_progress_map(progress_gdf, map_filename, verbose=verbose)
    # return
    # ################### DELETE THESE ###########################################

    num_updated_counter = 0
    started_downloads = False

    if verbose:
        print("Running through tiles to get to next tile to download...")

    for i, bbox in enumerate(bboxes):
        xmin_letter = "W" if (bbox[0] < 0) else "E"
        xmax_letter = "W" if (bbox[2] < 0) else "E"
        ymin_letter = "S" if (bbox[1] < 0) else "N"
        ymax_letter = "S" if (bbox[3] < 0) else "N"
        intro_message = "\n=== {0}/{1} === BBOX: {2}{3}-{4}{5}, {6}{7}-{8}{9}".format(
            i + 1,
            len(bboxes),
            abs(bbox[0]),
            "" if (xmin_letter == xmax_letter) else xmin_letter,
            abs(bbox[2]),
            xmax_letter,
            abs(bbox[1]),
            "" if (ymin_letter == ymax_letter) else ymin_letter,
            abs(bbox[3]),
            ymax_letter,
        )

        if track_progress:
            # Update the row and move along
            row = progress_df.loc[
                (progress_df.xmin == bbox[0])
                & (progress_df.ymin == bbox[1])
                & (progress_df.xmax == bbox[2])
                & (progress_df.ymax == bbox[3])
            ]

            try:
                assert len(row) == 1
            except AssertionError as e:
                print(progress_df)
                print("BBOX:", bbox)
                print(len(row))
                print(row)
                raise e

            if row.iloc[0]["is_downloaded"]:
                # Don't bother displaying this message until we get to the first one to download.
                if verbose and started_downloads:
                    print(intro_message)
                    print("Already downloaded. Moving on.")
                continue

        started_downloads = True

        success = False
        false_tries = 0
        while not success:
            print(intro_message)
            try:
                granules_list = nsidc_download.download_granules(
                    short_name=["ATL03", "ATL08"],
                    region=bbox,
                    local_dir=ICESAT2_DIR,
                    dates=["2021-01-01", ",2021-12-31"],
                    version="005",
                    fname_filter=None,
                    force=overwrite,
                    query_only=False,
                    fname_python_regex="\.h5\Z",
                    download_only_matching_granules=True,
                    skip_granules_if_photon_db_exists=True,
                    quiet=not verbose,
                )

                if isinstance(granules_list, Exception):
                    raise granules_list
                elif granules_list is None:
                    continue

                success = True
                false_tries = 0
            except KeyboardInterrupt as e:
                raise e
            except Exception as e:
                print("ERROR:\n\t", e)
                false_tries += 1

                if false_tries >= retry_count:
                    print("Errors. Waiting 5 minutes and then retrying...")
                    time.sleep(5 * 60)
                    false_tries = 0

        if track_progress:
            # Update the data record
            progress_df.loc[row.index, "is_downloaded"] = True
            progress_df.loc[row.index, "numgranules"] = len(granules_list)
            # Re-write the file out to disk. This helps if we cut this process short.
            progress_df.to_csv(progress_fname)
            if verbose:
                print(os.path.split(progress_fname)[1], "re-written.")

            num_updated_counter += 1

            # Only spit these out every 10 files.
            if num_updated_counter >= update_map_every_N:
                # Also write out a geopackage file, so we can visualize this.
                gpkg_fname = os.path.splitext(progress_fname)[0] + ".gpkg"
                progress_gdf = output_progress_csv_to_gpkg(
                    progress_df, gpkg_fname, verbose=verbose
                )

                map_filename = os.path.splitext(gpkg_fname)[0] + "_map.png"
                create_download_progress_map(
                    progress_gdf, map_filename, verbose=verbose
                )

                num_updated_counter = 0


def output_progress_csv_to_gpkg(progress_df, gpkg_fname, verbose=True):
    """I'm keeping track of the tiles downloaded using a CSV file. Export it to a gpkg to visualize."""

    # Temp function for creating a geometry polygon from the bounding-box values in our dataframe.
    def make_box(row):
        return shapely.geometry.box(row.xmin, row.ymin, row.xmax, row.ymax)

    # Create the geometry column.
    progress_df["geometry"] = progress_df.apply(make_box, axis=1)
    # Turn it into a GeoDataFrame
    progress_gdf = geopandas.GeoDataFrame(progress_df, geometry="geometry", crs=4326)
    # Write it out.
    progress_gdf.to_file(gpkg_fname, layer="download", driver="GPKG")
    if verbose:
        print(os.path.split(gpkg_fname)[1], "written.")
    return progress_gdf


def generate_photon_databases_from_existing_granules(
    overwrite=False,
    parallelize=True,
    numprocs=20,
    wait_time_when_zero_work_s=120,  # Wait 2 minutes before beginning again.
    delete_granules=True,
    verbose=True,
):
    """For all ATL08+ATL03 granule pairs that exist, generate a photon database.

    Both files must exist. Files can be downloaded from the download_all() function
    or using the nsidc_download.py module to get missing granules.

    If overwrite, generate a databaase even if it already exists. Otherwise, don't."""

    finished = False

    while not finished:
        if verbose:
            print(
                "Querying ICESat-2 directory for matching ATL03 & ATL08 granules that do not yet have photon databases."
            )

        granules_list = [
            fname
            for fname in os.listdir(ICESAT2_DIR)
            if (
                (fname[0:4] == "ATL0")
                and (os.path.splitext(fname)[1].lower() == ".h5")
                and (fname.find("_photons") == -1)
            )
        ]
        ATL03_granules = [gn for gn in granules_list if gn[0:5] == "ATL03"]
        ATL08_granules = [gn for gn in granules_list if gn[0:5] == "ATL08"]
        assert (len(ATL03_granules) + len(ATL08_granules)) == len(granules_list)

        ATL03_matching_granules = sorted(
            [
                os.path.join(ICESAT2_DIR, atl03)
                for atl03 in ATL03_granules
                if atl03.replace("ATL03", "ATL08") in ATL08_granules
            ]
        )
        ATL03_database_names = [
            atl03.replace(".h5", "_photons.h5") for atl03 in ATL03_matching_granules
        ]

        # If the _photons.h5 alreaady exists and we aren't overwriting, skip & move along.
        new_granules_to_create_pairs = [
            (granule, database)
            for (granule, database) in zip(
                ATL03_matching_granules, ATL03_database_names
            )
            if (overwrite or not os.path.exists(database))
        ]

        if len(new_granules_to_create_pairs) == 0:
            if wait_time_when_zero_work_s == 0:
                if verbose:
                    print("Nothing more to do. Exiting.")
                return

            time_mins = math.floor(wait_time_when_zero_work_s / 60)
            time_sec = int(math.remainder(wait_time_when_zero_work_s, 60))
            time_str = (
                ("" if time_mins == 0 else f"{time_mins} m")
                + (" " if ((time_mins > 0) and (time_sec > 0)) else "")
                + ("" if time_sec == 0 else f"{time_sec} s")
            )

            print(
                f"Nothing to do right now. Waiting {time_str} before trying again. (Hit Ctrl-C to end.)"
            )
            try:
                time.sleep(wait_time_when_zero_work_s)
            except KeyboardInterrupt:
                return
            # Then loop back around and try again.
            continue

        elif verbose:
            print(
                "Creating",
                len(new_granules_to_create_pairs),
                "new granule photon databases.",
            )

        if parallelize:
            procs_list = [None] * len(new_granules_to_create_pairs)
            N = len(procs_list)
            # First, generate a list of multiprocessing.Process objects to peruse through when doing this.
            for i, (granule, h5name) in enumerate(new_granules_to_create_pairs):
                procs_list[i] = multiprocessing.Process(
                    target=classify_icesat2_photons.save_granule_ground_photons,
                    args=(granule,),
                    kwargs={
                        "output_h5": h5name,
                        "overwrite": overwrite,
                        "delete_granules": delete_granules,
                        "verbose": False,
                    },
                )
            # Then, start executing those processes until they're all done.
            running_procs = []
            running_databases = []
            finished_procs = 0
            utils.progress_bar.ProgressBar(0, N, suffix="0/{0}".format(N))

            try:
                # Keep looping while there are still processes running or processes to yet finish.
                while (len(procs_list) > 0) or (len(running_procs) > 0):
                    # Sanity checks. These should always be true.
                    assert len(running_procs) == len(running_databases)
                    assert len(procs_list) == len(new_granules_to_create_pairs)

                    # First, loop through the list of running procs and see what's finished up.
                    for running_p, running_db in zip(running_procs, running_databases):
                        if not running_p.is_alive():
                            # If the process is finished. Join it, roll it up and toss it.
                            running_p.join()
                            running_p.close()
                            # Get rid of the proc, the db
                            running_procs.remove(running_p)
                            running_databases.remove(running_db)
                            finished_procs += 1
                            # Print the status update, and update the progress bar.
                            if verbose:
                                print("\r" + " " * 120, end="")
                                print(
                                    "\r{0}/{1} {2} written.".format(
                                        finished_procs, N, os.path.split(running_db)[1]
                                    )
                                )
                                utils.progress_bar.ProgressBar(
                                    finished_procs,
                                    N,
                                    suffix="{0}/{1}".format(finished_procs, N),
                                )

                    # Now loop through the unfinished procs and start them.
                    while (len(running_procs) < numprocs) and (len(procs_list) > 0):
                        proc = procs_list.pop(0)
                        (_, dbname) = new_granules_to_create_pairs.pop(0)
                        proc.start()
                        running_procs.append(proc)
                        running_databases.append(dbname)

                    time.sleep(0.05)

            except KeyboardInterrupt:
                # Kill the active processes and delete any databases that were currently being written to.
                for running_p, running_db in zip(running_procs, running_databases):
                    running_p.kill()
                    running_p.close()
                    if os.path.exists(running_db):
                        if verbose:
                            print(
                                "Deleting incomplete file", os.path.split(running_db)[1]
                            )
                        os.remove(running_db)
                finished = True

        else:
            try:
                for i, (granule, h5name) in enumerate(new_granules_to_create_pairs):
                    if verbose:
                        print(
                            "{0}/{1}".format(i + 1, len(new_granules_to_create_pairs)),
                            end=" ",
                        )
                    classify_icesat2_photons.save_granule_ground_photons(
                        granule, output_h5=h5name, overwrite=overwrite, verbose=verbose
                    )
            except KeyboardInterrupt:
                os.remove(h5name)
                finished = True

    return


def rebuild_progress_csv_from_gpkg(progress_csv_name):
    """Ooops, I fucked up KeyboardInterrupt'ing the process while it was writing download_progress.csv.
    I kept all the pertinent info in the gpkg anyway, so just rebuilt it from that."""
    gpkg_name = os.path.splitext(progress_csv_name)[0] + ".gpkg"
    gdf = geopandas.read_file(gpkg_name)
    print(gpkg_name, "read with", len(gdf), "records.")
    df = pandas.DataFrame(gdf.drop("geometry", axis=1))
    df.to_csv(progress_csv_name)
    print(progress_csv_name, "re-built with", len(df), "records.")
    return df


def generate_all_photon_tiles(
    map_interval=25, overwrite=False, numprocs=10, verbose=True
):
    """Run through creating all the icesat2_photon_database tiles.

    I could try to parallelize this, but each process would be writing to the same
    geodataframe database, and I don't feel like having to install conflict-handling code around that.
    So I won't unless it's necessary.
    """
    # Get all the icesat-2 photon database tiles. Use icesat2_photon_database object for this.
    # Assign a process to each tile generation, and then loop through running them.
    is2db = icesat2_photon_database.ICESat2_Database()
    # In case there are any unread textfiles written since the last go-round,
    # ingest them into the database.
    # NOTE: This takes a long time. Maybe don't do this regularly.
    # is2db.fill_in_missing_tile_entries(delete_csvs = True,
    #                                    save_to_disk = True,
    #                                    verbose=verbose)
    # gdf = is2db.open_gdf(verbose=verbose)

    if numprocs <= 0:
        numprocs = 1

    # Divide the globe up into 2x2* tiled boxes. The tiles help us make the best use
    # of disk-caching to maximize re-use of granule-photon databases between various
    # nearby tiles, rather than just mowing down a row by latitude or longitude.
    x_iter = numpy.arange(-180, 180, 2)
    y_iter = numpy.array(
        list(zip(numpy.arange(0, 90, 2), numpy.arange(-2, -92, -2)))
    ).flatten()
    xvals, yvals = numpy.meshgrid(x_iter, y_iter)

    N = len(is2db.open_gdf())
    if overwrite:
        N_so_far = 0
    else:
        N_so_far = len(
            [
                fname
                for fname in os.listdir(is2db.tiles_directory)
                if re.search("\Aphoton_tile_[\w\.]+\.((h5)|(feather))\Z", fname) != None
            ]
        )
    assert N >= N_so_far
    N_to_do = N - N_so_far
    if verbose:
        print(
            "Generating {0:,} total tiles, {1:,} already done, {2:,} left to go.".format(
                N, N_so_far, N_to_do
            )
        )

    tile_built_counter = 0
    tiles_built_since_last_map_output_counter = 0

    # This will hold the set of running processes.
    running_procs_list = []
    running_fnames_list = []
    mapping_process = None  # A variable specifically to store the sub-process that writes out the progress map
    writing_gpkg_process = None  # A variable specifically to store the sub-process that writes out the ICESat-2 database geopackage.

    delete_when_finished = False
    iters_since_last_delete_csvs = 0
    MAX_ITERS_SINCE_LAST_DELETE_CSVS = 4

    try:
        for xmin, ymin in zip(xvals.flatten(), yvals.flatten()):
            # For now, speed things up. We already know everything from -85 S to 90 N is finished, just skip ahead.
            # TODO: Delete later when finished, or when rebuilding the database.
            if not (ymin == 58 or ymin == -88):
                # if ymin >= -85:
                continue

            # A flag to see if any tiles have been actually generated in this box.
            xmax = xmin + 2
            ymax = ymin + 2
            # print(xmin, ymin, xmax, ymax)
            tiles_within_bbox = is2db.query_geopackage(
                (xmin, ymin, xmax, ymax), return_whole_records=True
            )
            if len(tiles_within_bbox) == 0:
                continue

            fnames = tiles_within_bbox["filename"].tolist()
            tile_xmins = tiles_within_bbox["xmin"].tolist()
            tile_xmaxs = tiles_within_bbox["xmax"].tolist()
            tile_ymins = tiles_within_bbox["ymin"].tolist()
            tile_ymaxs = tiles_within_bbox["ymax"].tolist()
            is_populated_list = tiles_within_bbox["is_populated"].tolist()

            for (
                tilename,
                tile_xmin,
                tile_xmax,
                tile_ymin,
                tile_ymax,
                is_populated,
            ) in zip(
                fnames,
                tile_xmins,
                tile_xmaxs,
                tile_ymins,
                tile_ymaxs,
                is_populated_list,
            ):
                # If the tile already exists and we're not overwriting, and it's already populated in the database, just skip it and move along.
                summary_csv_fname = os.path.splitext(tilename)[0] + "_summary.csv"
                if (
                    not overwrite
                    and (
                        os.path.exists(os.path.splitext(tilename)[0] + ".h5")
                        or os.path.exists(os.path.splitext(tilename)[0] + ".feather")
                    )
                    and (is_populated or os.path.exists(summary_csv_fname))
                ):
                    continue

                # We'll just keep looping until this process gets added onto the queue.
                process_started = False

                while not process_started:
                    # Loop through, first delete procs in the running_procs list that are finished.
                    for rproc, fname in zip(running_procs_list, running_fnames_list):
                        # If the process is finished up, close it out and remove it from the list.
                        if not rproc.is_alive():
                            rproc.join()
                            rproc.close()
                            running_procs_list.remove(rproc)
                            running_fnames_list.remove(fname)
                            # print("\r==== {0:,}/{1:,}".format(N_so_far + tile_built_counter + 1, N), os.path.split(tilename)[1], "====")
                            # Also give us a progress message, please.
                            if verbose:
                                print(
                                    "{0:,} / {1:,}".format(
                                        N_so_far + tile_built_counter + 1, N
                                    ),
                                    os.path.split(fname)[1],
                                    "written.",
                                )

                            tile_built_counter += 1
                            tiles_built_since_last_map_output_counter += 1

                    if (
                        isinstance(mapping_process, multiprocessing.Process)
                        and not mapping_process.is_alive()
                    ):
                        mapping_process.join()
                        mapping_process.close()
                        if verbose:
                            print(
                                os.path.split(is2db.get_tiling_progress_mapname())[1],
                                "written.",
                            )
                        mapping_process = None

                    if (
                        isinstance(writing_gpkg_process, multiprocessing.Process)
                        and not writing_gpkg_process.is_alive()
                    ):
                        writing_gpkg_process.join()
                        writing_gpkg_process.close()
                        if verbose:
                            print(
                                os.path.split(is2db.gpkg_fname)[1].strip(), "written."
                            )
                        writing_gpkg_process = None

                    # If we have space available in the running process queue, kick this off.
                    if len(running_procs_list) < numprocs:
                        # Create the new process with the args here.
                        # Save it as a feather file rather than a .h5 file.
                        tilename = tilename.replace(".h5", ".feather")
                        newproc = multiprocessing.Process(
                            target=is2db.create_photon_tile,
                            args=(
                                (tile_xmin, tile_ymin, tile_xmax, tile_ymax),
                                tilename,
                            ),
                            kwargs={
                                "overwrite": overwrite,
                                "write_stats": True,
                                "verbose": False,
                            },
                        )

                        # Kick the tires and light the fires.
                        newproc.start()

                        # Add it to the process queue and mark our flag.
                        running_fnames_list.append(tilename)
                        running_procs_list.append(newproc)
                        process_started = True

                    # Sleep for 1/100 th of a second, enough to keep this loop from eating CPU egregiously.
                    time.sleep(0.01)

                if (
                    tiles_built_since_last_map_output_counter >= map_interval
                ) and mapping_process is None:
                    if iters_since_last_delete_csvs >= MAX_ITERS_SINCE_LAST_DELETE_CSVS:
                        delete_when_finished = True

                    # First, just update it in memory, to keep the database updated.
                    # But don't do the slow part (saving to disk)
                    is2db.update_gpkg_with_csvfiles(
                        save_to_disk=False,
                        delete_when_finished=delete_when_finished,
                        verbose=verbose,
                    )

                    # is2db.fill_in_missing_tile_entries(save_to_disk = False,
                    #                                    delete_csvs = False,
                    #                                    verbose=False)
                    if verbose:
                        print(
                            "Kicking off another geodataframe and map output..."
                            + "\n\t(Also deleting _summary.csv's, so hold off on canceling 'till after GPKG is written.)"
                            if delete_when_finished
                            else ""
                        )

                    if delete_when_finished:
                        iters_since_last_delete_csvs = 0
                    else:
                        iters_since_last_delete_csvs += 1
                    delete_when_finished = False

                    writing_gpkg_process = multiprocessing.Process(
                        target=is2db.save_geopackage,
                        kwargs={
                            "use_tempfile": True,
                            "verbose": False,
                            "also_delete_redundant_csvs": False,
                        },
                    )
                    writing_gpkg_process.start()

                    # If it's time to create a new map, kick off the sub-process to do that.
                    # This is slow but we can let a separate process do it.
                    mapping_process = multiprocessing.Process(
                        target=create_tiling_progress_map,
                        kwargs={
                            "icesat2_db": is2db,
                            "update_with_csvs": False,
                            "use_tempfile": False,
                            "verbose": False,
                        },
                    )
                    mapping_process.start()
                    # create_tiling_progress_map(icesat2_db = is2db, verbose=verbose)
                    tiles_built_since_last_map_output_counter = 0
                    # total_tiles_done = numpy.count_nonzero(is2db.open_gdf()["is_populated"])

                    # if total_tiles_done > (N_so_far + tile_built_counter):
                    #     if verbose:
                    #         print(total_tiles_done - (N_so_far + tile_built_counter), "extra tiles found (likely generated elsewhere). Updating total.")
                    #     tile_built_counter = total_tiles_done - N_so_far

    except Exception as e:
        # Generally speaking, if we error out, it'll likely be during the (long) generation
        # of tile tile .h5 file. If we'd already created that file but hadn't yet created the _summary.csv,
        # then it is probably only a partially-written tile. Just get rid of it.
        for fname in running_fnames_list:
            if overwrite or (
                os.path.exists(fname)
                and not os.path.exists(os.path.splitext(fname)[0] + "_summary.csv")
            ):
                print("Deleting partial file", os.path.split(fname)[1])
                os.remove(fname)

        raise e


def convert_h5_to_feather(h5name, remove_h5=True, verbose=True):
    """Convert an .h5 database to a feather database.

    Feather has much faster read performance."""
    df = pandas.read_hdf(h5name)
    feather_name = os.path.splitext(h5name)[0] + ".feather"
    df.to_feather(feather_name)
    if verbose:
        print("Created", feather_name)
    if remove_h5:
        os.remove(h5name)
    if verbose:
        print("Removed", h5name)
    return feather_name


def inspect_photon_tiles_dir():
    """Count up the number of each type of file in the photon_tiles directory."""
    dirname = my_config._abspath(my_config.icesat2_photon_tiles_directory)
    fnames = sorted(os.listdir(dirname))

    h5_regex = r"photon_tile_[NS](\d{2})\.(\d{2})_[EW](\d{3})\.(\d{2})_[NS](\d{2})\.(\d{2})_[EW](\d{3})\.(\d{2})\.h5\Z"
    feather_regex = h5_regex.replace(".h5", ".feather")

    num_h5 = 0
    num_feather = 0
    num_non_matching = 0
    for fn in fnames:
        if re.search(h5_regex, fn) is not None:
            num_h5 += 1
        elif re.search(feather_regex, fn) is not None:
            num_feather += 1
        else:
            num_non_matching += 1
            print(fn)

    print()
    print(num_h5, ".h5 files.")
    print(num_feather, ".feather files.")
    print(num_non_matching, "non-matching files.")
    print(num_h5 + num_feather, ".h5 and .feather files combined.")


def clean_up_icesat2_photon_tile_database_and_directory():
    """Go through the entire photon_tiles directory and the database. Check:
    1) For duplicate files (.h5 and .feather). If both databases are valid, remove the .h5
    2) Invalid or corrupt database files. Remove the file and rebuild it.
    3) Empty database files, with zero records. Remove the file and remove the entry from the database.
    4) .h5 files. Go ahead and convert to feather. Update the database accordingly."""

    # tile_dirname = my_config._abspath(my_config.icesat2_photon_tiles_directory)
    # fnames = sorted(os.listdir(tile_dirname))
    # Get rid of the one EMPTY_TILE.h5 from the list. We don't need to consider this one.
    # fnames = [fn for fn in fnames if fn != "ATL03_EMPTY_TILE.h5"]

    is2db = icesat2_photon_database.ICESat2_Database()
    gdf = is2db.open_gdf()
    N = len(gdf)
    prev_str = ""
    for idx, row in gdf.iterrows():
        # (1) First, check to see if we have duplicate entries.
        num_files_matching = 0
        if os.path.exists(row.filename):
            num_files_matching += 1
        if os.path.exists(row.filename.replace(".h5", ".feather")):
            num_files_matching += 1

        if num_files_matching == 0:
            print("FILE NOT FOUND", row.filename)
        elif num_files_matching == 2:
            os.remove(row.filename)
            print(" " * len(prev_str) + "\r")
            print("Removed", row.filename)
            # df1 = pandas.read_hdf(row.filename)
            # df2 = pandas.read_feather(row.filename.replace(".h5", ".feather"))
            # If the two dataframe are equal to each other, just remove the .h5
            # if df1.equals(df2):
            #     os.remove(row.filename)
            #     print(" " * len(prev_str) + "\r")
            #     print("Removed", row.filename)
            # else:
            #
            #     print(" " * len(prev_str) + "\r")
            #     print("DATAFRAMES UNEQUAL:")
            #     print("  " + row.filename)
            #     print(df1)
            #     print("  " + row.filename.replace(".h5",".feather"))
            #     print(df2)

        # (2) Look for corrupt or invalid database files. Delete them if so.
        ph_df = is2db.read_photon_tile(row.filename)
        if len(ph_df) != row.numphotons:
            print(" " * len(prev_str) + "\r")
            print("MISMATCH", row.filename)
            print(" database:", row.numphotons)
            print(" file:", len(ph_df))

        prev_str = utils.progress_bar.ProgressBar(
            idx + 1, N, suffix="{0}/{1}".format(idx + 1, N)
        )


def convert_h5_to_feather_parallel(numprocs=10):
    """Convert remaining h5 files to feather files."""
    is2db = icesat2_photon_database.ICESat2_Database()
    gdf = is2db.open_gdf()

    print("Compiling list of .h5's to convert.")
    gdf["h5_exists"] = gdf.apply(lambda row: os.path.exists(row.filename), axis=1)

    print(" ", "{0:,}".format(numpy.count_nonzero(gdf.h5_exists)), "h5 files exist.")
    print(
        " ", "{0:,}".format(numpy.count_nonzero(~gdf.h5_exists)), "feather files exist."
    )

    h5_files = gdf.filename[gdf.h5_exists].to_list()
    feather_files = [os.path.splitext(h5)[0] + ".feather" for h5 in h5_files]
    args = [[h5] for h5 in h5_files]

    utils.parallel_funcs.process_parallel(
        convert_SINGLE_h5_to_feather,
        args,
        kwargs_list={"overwrite": True, "remove_h5_when_done": True},
        outfiles=feather_files,
        overwrite_outfiles=True,
        max_nprocs=numprocs,
        abbreviate_outfile_names_in_stdout=True,
        delete_partially_done_files=True,
    )


def convert_SINGLE_h5_to_feather(h5name, overwrite=False, remove_h5_when_done=True):
    base, ext = os.path.splitext(h5name)
    assert ext.lower() == ".h5"
    feather_name = base + ".feather"
    if os.path.exists(feather_name):
        if overwrite:
            os.remove(feather_name)
        else:
            return

    df = pandas.read_hdf(h5name)
    df.reset_index(drop=True, inplace=True)
    df.to_feather(feather_name)

    assert os.path.exists(feather_name)

    if remove_h5_when_done:
        os.remove(h5name)
    return


def define_and_parse_args():
    parser = argparse.ArgumentParser(
        description="Utility for downloading and pre-processing the whole fucking world of ICESat-2 photon data. Right now set to do the whole year 2021 (Jan 1-Dec 30)."
    )

    parser.add_argument(
        "-numprocs",
        "-np",
        default=0,
        type=int,
        help="The number of parallel processes to run. Default run sequentially on 1 process.",
    )
    parser.add_argument(
        "-map_interval",
        "-mi",
        default=10,
        type=int,
        help="When donwloading, the interval to update the map (ever N-th tile). Default 10.",
    )
    parser.add_argument(
        "-wait",
        "-w",
        default=120,
        type=int,
        help="In the 'generate_photon_databases' module, we go to sleep if there's nothing to do. Specify here how many seconds to sleep between retrying looking for granules to process.",
    )
    parser.add_argument(
        "--generate_photon_databases",
        "-g",
        action="store_true",
        default=False,
        help="For all pairs of granules that have both ATL03 and ALT08 downloaded, generate a photon database for that granule.",
    )
    parser.add_argument(
        "--generate_photon_tiling_progress_map",
        "-tm",
        action="store_true",
        default=False,
        help="Make an updated map of the photon tiling progress.",
    )
    # parser.add_argument("--move_icesat2_files", "-m", action="store_true", default=False, help="Move all the icesat-2 granuels and photon_databases from the old .cudem_cache directory into the data/icesat2/granules directory.")
    parser.add_argument(
        "--delete_redundant_granules",
        "-d",
        action="store_true",
        default=False,
        help="Delete ICESat-2 granules where a _photon.h5 database already exists. Free up some disk space.",
    )
    parser.add_argument(
        "--generate_all_photon_tiles",
        "-gat",
        action="store_true",
        default=False,
        help="Generate all the 0.25-degree photon tiles using the ICESat2 photon database code.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        default=False,
        help="Inspect the contents of the photon_tiles directory.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        default=False,
        help="Clean up and validate the photon_tiles database and directory.",
    )
    parser.add_argument(
        "--convert_h5_to_feather",
        "-ch5",
        action="store_true",
        default="False",
        help="Convert all existing .h5 database files to .feather files for faster reads.",
    )
    parser.add_argument(
        "--overwrite",
        "-o",
        action="store_true",
        default=False,
        help="Overwrite existing files.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", default=False, help="Run in quiet mode."
    )

    return parser.parse_args()


if __name__ == "__main__":
    # Temporarily uncomment to create the icesat-2 download progress csv.
    # create_icesat2_progress_csv(from_gpkg=True)
    # delete_granules_where_photon_databases_exist()
    # print("Running with subset_existing_photon_databases_to_ground_and_canopy_only() enabled.")

    # subset_existing_photon_databases_to_ground_and_canopy_only()
    # import sys
    # sys.exit(0)

    args = define_and_parse_args()

    # if args.move_icesat2_files:
    #     move_all_granules_to_icesat2_granules_dir()

    #     if args.delete_redundant_granules:
    #         delete_granules_where_photon_databases_exist()

    if args.inspect:
        inspect_photon_tiles_dir()

    elif args.clean:
        clean_up_icesat2_photon_tile_database_and_directory()

    elif args.convert_h5_to_feather:
        convert_h5_to_feather_parallel(numprocs=args.numprocs)

    elif args.generate_all_photon_tiles:
        generate_all_photon_tiles(
            map_interval=args.map_interval,
            overwrite=args.overwrite,
            numprocs=args.numprocs,
            verbose=not args.quiet,
        )

    elif args.generate_photon_tiling_progress_map:
        create_tiling_progress_map(verbose=not args.quiet)

    elif args.generate_photon_databases:
        generate_photon_databases_from_existing_granules(
            parallelize=not (args.numprocs == 0),
            numprocs=args.numprocs,
            wait_time_when_zero_work_s=args.wait,
            overwrite=args.overwrite,
            verbose=not args.quiet,
        )

        if args.delete_redundant_granules:
            delete_granules_where_photon_databases_exist()

    elif args.delete_redundant_granules:
        delete_granules_where_photon_databases_exist()

    else:
        download_all(
            overwrite=args.overwrite,
            update_map_every_N=args.map_interval,
            verbose=not args.quiet,
        )
