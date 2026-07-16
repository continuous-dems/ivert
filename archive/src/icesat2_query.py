import argparse
import ast
import dateparser
import datetime
import geopandas
import numpy
import os
import pandas
import pyproj
import rasterio
import rasterio.warp
import rasterio.crs
import re
import shapely
import sys
import traceback
import typing
import xarray

from cudem import regions
from cudem.datalists import icesat2file

# from cudem.fetches import earthdata, fetches
import utils.pickle_blosc
import icesat2_requests
import coastline_mask_v2

# For reference, the various photon dataframe classifications.
photon_classes_dict = {
    -1: "unclassified",
    0: "atmosphere/noise",
    1: "ground",
    2: "canopy",
    3: "canopy top",
    6: "ice surface",  # Currently unused, will use in ATL06 integration
    7: "building",  # Using either Bing Building Footprints or OSM outlines
    8: "urban",  # Only if using World Settlement Footprint
    40: "bathymetry floor",
    41: "bathymetry surface",
}

photon_classes_dict_reverse = dict(
    [(val, key) for (key, val) in list(photon_classes_dict.items())]
)


def get_photon_dataframe(
    polygon_bbox_or_dem_fname: typing.Union[shapely.geometry, list, tuple, str],
    dem_horz_reference_frame: typing.Union[str, None, pyproj.CRS, int] = "EPSG:4326",
    dem_vert_reference_frame: typing.Union[str, None, pyproj.CRS, int] = "EPSG:3855",
    start_date: str = "a year ago midnight",
    end_date: str = "midnight today",
    other_columns: typing.Union[dict, None] = None,
    classify_bathymetry: bool = True,
    classify_buildings: bool = True,
    classifications_to_keep: typing.Union[list, tuple, str, set, numpy.ndarray] = (
        1,
        2,
        3,
        7,
        40,
        41,
    ),
    conf_levels_to_keep: typing.Union[list, tuple, str, set, numpy.ndarray] = "4",
    download_directory: typing.Union[str, None] = None,
    # merge_granules: bool = True,
) -> typing.Union[pandas.DataFrame, list[pandas.DataFrame], None]:
    """Return a dataframe of all classified photons within the polygon/bounding-box and timeframe, from NSIDC data.

    Photons will be returned in the vertical and horizontal reference frame specified by dem_horz_reference_frame and
    dem_vert_reference_frame, respectively.

    The original X,Y,Z coordinates (in WGS84 (ITRF 2014) lat/lon and EGM2008 height) will be in columns

    Parameters:
        polygon_bbox_or_dem_fname: A shapely.geometry.Polygon object, or bounding-box is a 4-value list/tuple
            of (xmin,xmax,ymin,ymax). Coordinates should be in the reference frame outlined in dem_horz_reference_frame.
            Alternately, can also be the name of a DEM file from which to derive the bounding box.
        dem_horz_reference_frame: The horizontal reference frame of the DEM. Default None.
            If None, attempt to derive the horizontal reference frame from the DEM file if a filename is provided in
            the parameter polygon_bbox_or_dem_fname. This is None and no filename is provided, throws a ValueError.
        dem_vert_reference_frame: The vertical reference frame of the DEM. Can be any value suppored by cudems.vdatum.
            Run "vdatums --list-epsg" for a full list of options. Default None.
            If None, attempt to derive the vertical reference frame from the DEM file if a filename is provided in
            the parameter polygon_bbox_or_dem_fname. If unsuccessful, it will attempt to get it from the horizontal
            reference frame, in case it is a compound reference frame. If unsuccessful, it will throw a ValueError.
        start_date: The start date of the time period to query. Can be any string parsed by dateparser.parse().
            Default 'a year ago midnight'.
        end_date: The end date of the time period to query. Can be any string parsed by dateparser.parse().
            Default 'midnight today'. If end_date is before start_date, will throw a ValueError.
        other_columns: A dictionary of other columns to include in the dataframe. It should include the ATL03 variable
            path followed by the column name you want to assign it. For example {'/heights/h_ph':'h_ph'} Default None.
        classify_bathymetry: Whether to classify the bathymetry. Default True, which will run CShelph.
        classify_buildings: Whether to classify the buildings. Default True, which will use Bing building footprints.
        classifications_to_keep: A list of the photon classifications to keep. Default (1, 2, 3, 7, 40, 41).
            See "dlim --modules icesat2" for a list of classification numbers.
        conf_levels_to_keep: A list of the photon confidence levels to keep.
            Default "4" (only keep highest-confidence photons). Can also be a set, tuple, or numpy.ndarray. If a string,
            values should be integers 0-4 seperated by forward slashes (no spaces).
        download_directory: The directory in which to download ICESat-2 data.
            Default None, which will use the current working directory.
        merge_granules: bool
            Whether to return one single dataframe, or a list of dataframes (one for each granule). Default True.

    Raises:
        ValueError: if dem_horz_reference_frame is None, and no filename is provided in the
            parameter polygon_bbox_or_dem_fname. Or if the start_date is on or after the end_date.

    Returns:
        A pandas.DataFrame of all classified photons within the polygon/bounding-box and timeframe.
        None if no ICESat-2 data is returned. If merge_granules is False, return a list of the resultant dataframes.
    """

    # Extract the dem_horz and def_vert reference frames from either the file or the user inputs.
    horz_proj, vert_proj = get_dem_horz_and_vert_reference_frame(
        polygon_bbox_or_dem_fname, dem_horz_reference_frame, dem_vert_reference_frame
    )
    assert horz_proj is not None and vert_proj is not None

    # Make sure the date strings are valid and aren't on the same day or in reverse order.
    start_datestr = _process_input_date_str(start_date)
    end_datestr = _process_input_date_str(end_date)
    if start_datestr >= end_datestr:
        raise ValueError(
            "The start date must occur one or more days before the end date."
        )

    # Get the list of classifications to keep as a "0/1/2/3" type of string of integers separated by forward-slashes.
    classes_str = _get_classifications_str(classifications_to_keep)
    conf_str = _get_classifications_str(conf_levels_to_keep)

    dem_wgs84_bbox = get_wgs84_bounding_box(polygon_bbox_or_dem_fname, horz_proj)
    region = regions.Region().from_list(list(dem_wgs84_bbox))

    fetches_module = (
        f"icesat2:time_start={start_datestr}:time_end={end_datestr}:subset=True"
    )

    # Get a short string for the srs of the DEM, from the two projections above.
    dem_srs_string = get_dem_srs_string(horz_proj, vert_proj)
    # ICESat-2, by default using dlim, comes in WGS84 lat-lon coordinates and EGM2008 vertical reference frame.
    is2_srs_string = "EPSG:4326+3855"

    print(
        f"ds = dlim.IceSat2Fetcher(fn='{repr(fetches_module)}', src_region='{repr(region)}', src_srs='{repr(is2_srs_string)}', "
        f"dst_srs={repr(dem_srs_string)}', classes={repr(classes_str)}, confidence_levels={repr(conf_str)}, "
        f"columns={repr(other_columns if other_columns else {})}, classify_bathymetry={repr(classify_bathymetry)}, "
        f"classify_buildings={classify_buildings})"
    )

    old_pwd = os.getcwd()
    if download_directory is not None:
        if not os.path.exists(download_directory):
            os.makedirs(download_directory, exist_ok=True)
        os.chdir(download_directory)

    # TODO: Fix this code. It currently uses the previous (cudem v2.6.0) API syntax. See "download_granules()" below
    #  to fix this.
    # Initialize the icesat2 query.
    ds = dlim.DatasetFactory(
        mod=fetches_module,
        src_region=region,
        src_srs=is2_srs_string,
        dst_srs=dem_srs_string,
        classes=classes_str,
        confidence_levels=conf_str,
        columns=other_columns if other_columns else {},
        classify_bathymetry=classify_bathymetry,
        classify_buildings=classify_buildings,
        reject_failed_qa=True,
    )._acquire_module()

    # Run the icesat2 query over all files and lasers in that box.
    list_of_dfs = []

    for i, df in enumerate(ds.initialize().yield_points()):
        # Drop any rows that have NaN values.
        df.dropna(inplace=True)
        # Drop columns we don't need.
        df.drop(columns=["ref_elevation", "ref_azimuth", "ref_sat_alt"], inplace=True)
        # Rename the column "ph_h_classed" to "class_code"
        df.rename(columns={"ph_h_classed": "class_code"}, inplace=True)

        # If there are any points remaining after this, append the dataframe to the list.
        if len(df) > 0:
            list_of_dfs.append(df)

    # If we'd changed our working directory, then move us back to the original working directory.
    if download_directory is not None:
        os.chdir(old_pwd)

    if len(list_of_dfs) == 0:
        # if merge_granules:
        #     return []
        # else:
        return None

    # if merge_granules:
    # Merge the dataframes together.
    df_out = pandas.concat(list_of_dfs, ignore_index=True)

    df_out = _filter_points_against_dem(df_out, polygon_bbox_or_dem_fname, horz_proj)

    if len(df_out) == 0:
        return None
    else:
        return df_out

    # else:
    #     return [_filter_points_against_dem(df, polygon_bbox_or_dem_fname, horz_proj) for df in list_of_dfs]


def clean_bbox(bbox: typing.Union[list, tuple]):
    """Error check and convert data-types to return a valid 6-tuple bounding box in the following format:
    (xmin [float], xmax [float], ymin [float], ymax [float], tmin [int], tmax [int])
    """
    bbox = list(bbox)
    if len(bbox) != 6:
        raise ValueError(
            "bbox must contain exactly 6 values, (xmin, xmax, ymin, ymax, tmin, tmax)"
        )
    if bbox[0] >= bbox[1]:
        raise ValueError("xmin must be less than xmax")
    if bbox[2] >= bbox[3]:
        raise ValueError("ymin must be less than ymax")

    # If the tmin is not an integer, make it one.
    try:
        bbox[4] = int(bbox[4])
        assert len(str(bbox[4])) == 8
    except (ValueError, AssertionError):
        raise ValueError("tmin must be an 8-digit YYYYMMDD integer")

    # If the tmax is not an integer, make it one.
    try:
        bbox[5] = int(bbox[5])
        assert len(str(bbox[5])) == 8
    except (ValueError, AssertionError):
        raise ValueError("tmin must be an 8-digit YYYYMMDD integer")

    # Make sure the tmin is greater than the tmax.
    if bbox[4] >= bbox[5]:  # Comparison works with strings or integers, either way.
        raise ValueError("tmin must be less than tmax")

    return bbox


def find_existing_granules(atl03_granule, bbox, search_directory):
    """Return a list of all existing IVERT granules that have already been created from the atl03 granule.

    The list will be anywhere from 0 to 6 items long."""
    all_cdf_files = [
        fn
        for fn in os.listdir(search_directory)
        if os.path.splitext(fn)[1].lower() == ".cdf"
    ]

    granule_id = str(
        os.path.basename(atl03_granule).replace("_subsetted", "").replace(".h5", "")
    )
    granule_ending = (
        f"_{'W' if bbox[0] < 0 else 'E'}{abs(bbox[0]):0.5f}_"
        + f"{'W' if bbox[1] < 0 else 'E'}{abs(bbox[1]):0.5f}_"
        + f"{'S' if bbox[2] < 0 else 'N'}{abs(bbox[2]):0.5f}_"
        + f"{'S' if bbox[3] < 0 else 'N'}{abs(bbox[3]):0.5f}_"
        + f"{bbox[4]:d}_"
        + f"{bbox[5]:d}"
        + ".cdf"
    )

    return [
        fn
        for fn in all_cdf_files
        if fn.startswith(granule_id) and fn.endswith(granule_ending)
    ]


def download_granules(
    bbox: typing.Union[list, tuple],
    output_directory: str,
    download_directory: typing.Union[str, None] = None,
    classifications_to_keep: typing.Union[list, tuple, str] = (1, 2, 3, 7, 40, 41),
    conf_levels_to_keep: typing.Union[list, tuple, int, str] = "4",
    other_columns: typing.Union[dict, None] = None,
    classify_water: bool = True,
    classify_buildings: bool = True,
    min_bathy_confidence: typing.Union[float, None] = 0.0,
    trim_to_bbox: bool = False,
    overwrite: bool = True,
) -> pandas.DataFrame:
    """Download granules from the ICESat-2 API over an (x,y,t) bounding box in WGS84 format.

    Parameters:
        bbox : list or tuple (6 entries)
            (xmin, xmax, ymin, ymax, tmin, tmax) in WGS84 coordinates and YYYYMMDD integers
        output_directory: where to write the final classified granules.
        download_directory: where to write the ICESat-2 data as it's being downloaded
            If None, put in the same directory as the output_directory
        classifications_to_keep: list of classifications to keep (0,1,2,3,7,40,41)
        conf_levels_to_keep: list of confidence levels to keep (1,2,3,4)
        other_columns: dict of other columns to keep (default: None)
            For example: {"/gtx/heights/delta_time": "delta_time"}
        classify_water: whether to classify bathymetry using ATL24 data (default: True)
        classify_buildings: whether to classify buildings using Bing footprints (default: True)
        min_bathy_confidence: minimum confidence level for bathymetry classification, 0.0 - 1.0. (default: 0.0).
        trim_to_bbox: whether to trim the data within the output granules to the bbox before returning (default: True)
        subset_granules: whether to subset the granules to the bbox in Earthdata Harmony when downloading (default: True)
        previous_job_id: Use a previous 30-digit job ID from a NASA Earthdata request instead of making a new one.
            It is up to the user to guarantee that previous request was over the same bbox as the current request.
        overwrite: whether to overwrite existing granules (default: True)

    Returns:
        pandas.DataFrame from the metadata of each table written to disk.

    Raises:
        ValueError: If bbox does not contain exactly 6 values or if values are misordered (min less than max)
        OSError: If output_directory cannot be created or written to
    """
    ##################
    ## Validate inputs
    ##################
    bbox = clean_bbox(bbox)

    # Create download and output directories if they don't exist.
    if not os.path.exists(output_directory):
        print("Creating output directory:", output_directory)
        os.makedirs(output_directory, exist_ok=True)

    if download_directory is None:
        download_directory = output_directory
    elif not os.path.exists(download_directory):
        os.makedirs(download_directory, exist_ok=True)

    # All ICESat-2 data is given in EPSG:4326+3855. Use this to store it, and convert to other SRS/datums when
    # validating.
    icesat2_srs = "EPSG:4326+3855"

    # Generate a request object. This will use the "requests.csv" file to re-use any previous requests over
    # the same bounding boxes.
    is2_request = icesat2_requests.ICESat2Request(bbox, download_directory)

    # Make the requests on the Harmony service (or use previous existing requests if still valid).
    is2_request.make_icesat2_request(use_previous_if_matching=True, verbose=True)

    # Do the coastline mask masking while the ICESat-2 request is processing.
    if classify_water:
        # Download the water masks, both for ocean and inland waters.
        ocean_mask_fn = coastline_mask_v2.OSMOceanMask(
            os.path.join(download_directory, "osm_ocean"), bbox=bbox
        ).download(overwrite=overwrite, verbose=True)

        inland_water_mask_fn = coastline_mask_v2.OSMInlandWaterMask(
            os.path.join(download_directory, "osm_inland_water"), bbox=bbox
        ).download(overwrite=overwrite, verbose=True)
    else:
        # Else just set these as boolean flags to "False", to let icesat2file.IceSat2_ATL03() know not to do those classifications.
        ocean_mask_fn = False
        inland_water_mask_fn = False

    if classify_buildings:
        # Download the Bing BFP mask.
        buildings_mask_fn = coastline_mask_v2.BingBFPMask(
            os.path.join(download_directory, "bingbfp"), bbox=bbox
        ).download(overwrite=overwrite, verbose=True)
    else:
        buildings_mask_fn = False

    # Download all the ICESat-2 granules we need.
    is2_request.download_granules(
        overwrite=False, wait_until_complete=True, verbose=True
    )

    if not is2_request.are_requests_finished():
        print("ERROR:")
        for short_name in sorted(is2_request.json_dict.keys()):
            print(short_name + ":", is2_request.json_dict[short_name])
        raise RuntimeError("Requests did not finish.")

    # Now process the granules.
    granule_names = is2_request.granule_filenames()
    region = regions.Region().from_list(bbox[0:4])

    # Create an "already_processed.txt" file to use for caching results from previously-processed granules. Useful
    # if it dies and we have to restart it.
    already_processed_fn = os.path.join(download_directory, "already_processed.txt")
    if os.path.exists(already_processed_fn):
        already_processed_list = [
            line.strip()
            for line in open(already_processed_fn, "r").readlines()
            if len(line.strip()) > 0
        ]
    else:
        already_processed_list = []

    # A list of metadata objects to be added to the icesat2_database_v2 database for documentation of this dataset.
    list_of_metadata_dicts = []

    if download_directory is not None:
        old_cwd = os.getcwd()
        os.chdir(download_directory)
    else:
        old_cwd = None

    for i, atl03_granule in enumerate(granule_names):
        gname = os.path.basename(atl03_granule)

        # If there are already 6 IVERT granules processed for this ATL granule (one for each laser), then I can skip
        # onto the next iteration.
        if len(find_existing_granules(atl03_granule, bbox, output_directory)) == 6:
            # Add this file to the list of already-processed files to avoid processing again later if we have to restart.
            if gname not in already_processed_list:
                already_processed_list.append(gname)
                with open(already_processed_fn, "w") as f:
                    f.write("\n".join(already_processed_list))

        # If we're not overwriting files, skip if we've already processed this granule.
        if not overwrite and gname in already_processed_list:
            print(gname, "has already been processed.")
            continue

        try:
            ds = icesat2file.IceSat2_ATL03(
                fn=atl03_granule,
                src_region=region,
                src_srs=icesat2_srs,
                dst_srs=icesat2_srs,
                classes=_get_classifications_str(classifications_to_keep),
                confidence_levels=conf_levels_to_keep,
                columns=other_columns,
                classify_water=ocean_mask_fn,
                classify_inland_water=inland_water_mask_fn,
                classify_buildings=buildings_mask_fn,
                min_bathy_confidence=min_bathy_confidence,
            ).initialize()

            # Run the icesat2 query over all files and lasers in that box.
            for df in ds.yield_points():
                if len(df) == 0:
                    continue

                # Fetch the laser_name and the filename
                laser_name = df["laser"].iloc[0]
                fn = df["fn"].iloc[0]

                # Get the basic granule ID. This will be our basename for the granule file.
                granule_id = (
                    os.path.basename(fn).replace("_subsetted", "").replace(".h5", "")
                )

                # Construct our output filename for this granule, with the bbox.
                outfile_name = os.path.join(
                    output_directory,
                    f"{granule_id}_{laser_name}_"
                    f"{'W' if bbox[0] < 0 else 'E'}{abs(bbox[0]):0.5f}_"
                    f"{'W' if bbox[1] < 0 else 'E'}{abs(bbox[1]):0.5f}_"
                    f"{'S' if bbox[2] < 0 else 'N'}{abs(bbox[2]):0.5f}_"
                    f"{'S' if bbox[3] < 0 else 'N'}{abs(bbox[3]):0.5f}_"
                    f"{bbox[4]:d}_"
                    f"{bbox[5]:d}"
                    ".cdf",
                )

                if os.path.exists(outfile_name):
                    if overwrite:
                        print(f"Overwriting existing granule: {outfile_name}")
                        os.remove(outfile_name)
                    else:
                        print(f"Skipping existing granule: {outfile_name}")
                        continue

                # Make sure all the laser names and filenames are identical in each returned table
                assert len(list(df["laser"].unique())) == 1
                assert len(list(df["fn"].unique())) == 1

                # Get rid of fields we don't need in our dataframe
                df.drop(
                    columns=[
                        "laser",
                        "fn",
                        "ref_elevation",
                        "ref_azimuth",
                        "ref_sat_alt",
                    ],
                    axis="columns",
                    inplace=True,
                )
                # Rename our "ph_h_classed" field to "class_code"
                df.rename(columns={"ph_h_classed": "class_code"}, inplace=True)

                # If requested, mask out data that lies outside the bounding box queried.
                if trim_to_bbox:
                    query_xmin, query_xmax = bbox[0], bbox[1]
                    query_ymin, query_ymax = bbox[2], bbox[3]
                    query_tmin, query_tmax = bbox[4], bbox[5]
                    query_dtime_min = yyyymmdd_to_delta_time(query_tmin)
                    query_dtime_max = yyyymmdd_to_delta_time(query_tmax)

                    valid_mask = (
                        (df["x"] >= query_xmin)
                        & (df["x"] < query_xmax)
                        & (df["y"] >= query_ymin)
                        & (df["y"] < query_ymax)
                        & (df["delta_time"] >= query_dtime_min)
                        & (df["delta_time"] < query_dtime_max)
                    )

                    valid_count = numpy.count_nonzero(valid_mask)
                    if valid_count < len(df):
                        # Shorten these messages later. Right now for testing.
                        print(
                            f"\nTrimming dataframe from {len(df):,} to {valid_count:,} points within bounding-box ({query_xmin:f}, {query_xmax:f}, {query_ymin:f}, {query_ymax:f}, {query_tmin:d}, {query_tmax:d})."
                        )
                        # print(f"Delta times: {query_dtime_min} to {query_dtime_max}")
                        # print(f"Total data range: ({df['x'].min():f}, {df['x'].max():f}, {df['y'].min():f}, {df['y'].max():f}, {df['delta_time'].min():f}, {df['delta_time'].max():f})")
                    if valid_count == 0:
                        continue

                    df = df[valid_mask]

                # Convert floating point and long-int fields with a short int8 (these are only positive values from 0-8)
                convert_dict = {"class_code": numpy.int8, "confidence": numpy.int8}

                # Convert the fields and reset the indices
                df = df.astype(convert_dict).reset_index()

                # Get the actual bbox of the data (not just the query bbox)
                data_xmin = df["x"].min()
                data_xmax = df["x"].max()
                data_ymin = df["y"].min()
                data_ymax = df["y"].max()

                try:
                    granule_yyyymmdd = int(
                        re.search(r"(?<=\AATL03_)\d{8}(?=\d)", granule_id).group()
                    )
                    assert 2018_00_00 <= granule_yyyymmdd <= 3000_00_00
                except AttributeError:
                    raise ValueError(
                        f"granule_id '{granule_id}' does not contain a YYYYMMDD integer in the proper place."
                    )

                # The first and last time YYYYMMDD values.
                data_tmin = delta_time_to_yyyymmdd(df["delta_time"].iloc[0])
                data_tmax = delta_time_to_yyyymmdd(df["delta_time"].iloc[-1])
                assert data_tmin <= data_tmax

                data_zmin = df["z"].min()
                data_zmax = df["z"].max()

                # Convert the photon dataframe to an xarray Dataset
                xds = xarray.Dataset.from_dataframe(df)
                # Create a metadata dictionary, this will be added to the metadata dataframe about each tile.
                metadata_dict = {
                    "granule_id": granule_id,
                    "laser_name": laser_name,
                    "query_bbox": bbox,
                    "data_bbox": [
                        data_xmin,
                        data_xmax,
                        data_ymin,
                        data_ymax,
                        data_tmin,
                        data_tmax,
                    ],
                    "zbounds": [data_zmin, data_zmax],
                    "numphotons": len(df),
                    "numphotons_unclassified": numpy.count_nonzero(
                        df["class_code"] == photon_classes_dict_reverse["unclassified"]
                    ),
                    "numphotons_noise": numpy.count_nonzero(
                        df["class_code"]
                        == photon_classes_dict_reverse["atmosphere/noise"]
                    ),
                    "numphotons_ground": numpy.count_nonzero(
                        df["class_code"] == photon_classes_dict_reverse["ground"]
                    ),
                    "numphotons_canopy": numpy.count_nonzero(
                        df["class_code"] == photon_classes_dict_reverse["canopy"]
                    ),
                    "numphotons_canopy_top": numpy.count_nonzero(
                        df["class_code"] == photon_classes_dict_reverse["canopy top"]
                    ),
                    "numphotons_bathy_floor": numpy.count_nonzero(
                        df["class_code"]
                        == photon_classes_dict_reverse["bathymetry floor"]
                    ),
                    "numphotons_bathy_surface": numpy.count_nonzero(
                        df["class_code"]
                        == photon_classes_dict_reverse["bathymetry surface"]
                    ),
                    "numphotons_buildings": numpy.count_nonzero(
                        df["class_code"] == photon_classes_dict_reverse["building"]
                    ),
                    "crs": icesat2_srs,
                    "downloaded_on": int(datetime.datetime.now().strftime("%Y%m%d")),
                }

                # Save the metadata to the attributes of the photon dataset. This allows the parent table to be recreated
                # by reading the attributes of each photon .cdf file and reconstructing the geodataframe from it. That work
                # is done in icesat2_database_v2.py
                xds.attrs = metadata_dict.copy()

                # Write the photon netcdf to disk.
                try:
                    xds.to_netcdf(outfile_name)
                except Exception as e:
                    os.remove(outfile_name)
                    raise e

                if os.path.exists(outfile_name):
                    print(
                        f"\nWrote granule {os.path.basename(outfile_name)} with {len(df)} points."
                    )
                else:
                    raise OSError(
                        f"Failed to write granule {os.path.basename(outfile_name)}"
                    )

                metadata_dict["filename"] = os.path.basename(outfile_name)

                # Add to the list of metadata dicts for files written.
                list_of_metadata_dicts.append(metadata_dict)

        except KeyboardInterrupt as kb:
            raise kb
        except Exception as e:
            traceback.print_exception(e)
            print("Continuing...")
            continue

        # Add this file to the list of already-processed files to avoid processing again later if we have to restart.
        if gname not in already_processed_list:
            already_processed_list.append(gname)
            with open(already_processed_fn, "w") as f:
                f.write("\n".join(already_processed_list))

    # If we moved working directories previously, move us back.
    if old_cwd is not None:
        os.chdir(old_cwd)

    # Create a dataframe from the list of metadata dicts and return it. The parent function can turn this into a
    # geodataframe if it wants.
    return pandas.DataFrame.from_dict(list_of_metadata_dicts)


def get_dem_srs_string(horz_reference: pyproj.CRS, vert_reference: pyproj.CRS) -> str:
    """Get the short string for the projection from the projections.

    This assumes a valid SRS projection has been found for both the horizontal and vertical components. If it is a 3D
    reference frame they should be the same (as returned by get_dem_horz_and_vert_reference_frame), and that the
    authority (EPSG, etc) of both reference frames should be the same.

    Raises:
        ValueError if the two datums are based on different authorities.

    Returns:
        string in the format ("AUTH:HORZ+VERT")
    """
    horz_auth = horz_reference.list_authority()[0].auth_name.upper()
    vert_auth = vert_reference.list_authority()[0].auth_name.upper()

    if horz_auth != vert_auth:
        raise ValueError(
            "Reference authorities for the horizontal datum and vertical datum must match."
        )

    if horz_reference.equals(vert_reference):
        return horz_reference.srs
    else:
        return f"{horz_auth}:{horz_reference.list_authority()[0].code}+{vert_reference.list_authority()[0].code}"


def get_dem_horz_and_vert_reference_frame(
    polygon_bbox_or_dem_fname: str,
    dem_horz_reference_frame: typing.Union[str, None],
    dem_vert_reference_frame: typing.Union[str, None],
) -> typing.Tuple:
    """From the user inputs, get the horizontal and vertical reference frame of the DEM.

    Use the user-inputs if they're defined, otherwise attempt to get them from the DEM file (if provided).

    Parameters:
        polygon_bbox_or_dem_fname: A shapely.geometry.Polygon object, or bounding-box is a 4-value list/tuple
            of (xmin,xmax,ymin,ymax). Coordinates should be in the reference frame outlined in dem_horz_reference_frame.
            Alternately, can also be the name of a DEM file from which to derive the bounding box.
        dem_horz_reference_frame: The horizontal reference frame of the DEM.
        dem_vert_reference_frame: The vertical reference frame of the DEM. Can be any value suppored by cudems.vdatum.
            Run "vdatums --list-epsg" for a full list of options.

    Raises:
        ValueError: if dem_horz_reference_frame is None, and no filename is provided in the
            parameter polygon_bbox_or_dem_fname.

    Returns:
        A tuple of (dem_horz_reference_frame, dem_vert_reference_frame) as pyproj.CRS objects.
    """

    # First, get a lat-lon bounding box from the "polygon_bbox_or_dem_fname" parameter.
    if (
        dem_horz_reference_frame is None or dem_vert_reference_frame is None
    ) and not os.path.exists(polygon_bbox_or_dem_fname):
        raise ValueError(
            "If no filename is provided in the parameter polygon_bbox_or_dem_fname, "
            "dem_horz_reference_frame and dem_vert_reference_frame must be specified."
        )

    # Get the DEM reference frames, both horizontal and vertical
    if (dem_horz_reference_frame is None) and (dem_vert_reference_frame is None):
        dem_horz_reference_frame, dem_vert_reference_frame = (
            get_dem_reference_frame_from_file(
                polygon_bbox_or_dem_fname, vert_horz_or_both="both"
            )
        )

    # If a value is None, then attempt to get it from the file.
    # If a value is a caller-created string, then derive the reference frame from that.
    elif dem_horz_reference_frame is None:
        # If only the DEM's vertical reference frame is given here, then get the horizontal one from the file.
        dem_horz_reference_frame = get_dem_reference_frame_from_file(
            polygon_bbox_or_dem_fname, vert_horz_or_both="horz"
        )
        # Get the vertical reference frame from the user input
        dem_vert_reference_frame = get_dem_reference_frame_from_user_input(
            dem_vert_reference_frame, vert_horz_or_both="vert"
        )

    elif dem_vert_reference_frame is None:
        # If only the DEM's horizontal reference frame is given here, then get the vertical one from the file.
        dem_vert_reference_frame = get_dem_reference_frame_from_file(
            polygon_bbox_or_dem_fname, vert_horz_or_both="vert"
        )
        # Get the horizontal reference frame from the user input
        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame, vert_horz_or_both="horz"
        )
    else:
        # Get the horizontal and vertical reference frames from the user input
        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame, vert_horz_or_both="horz"
        )
        dem_vert_reference_frame = get_dem_reference_frame_from_user_input(
            dem_vert_reference_frame, vert_horz_or_both="vert"
        )

    # At this point, we should have a valid vertical and horizontal reference frame to be working from.
    if dem_horz_reference_frame is None or dem_vert_reference_frame is None:
        prefix_text = ""
        if dem_horz_reference_frame is None:
            prefix_text = (
                prefix_text
                + "dem_horz_reference_frame is not specified or could not be derived.\n"
            )
        if dem_vert_reference_frame is None:
            prefix_text = (
                prefix_text
                + "dem_vert_reference_frame is not specified or could not be derived.\n"
            )
        raise ValueError(
            prefix_text
            + "dem_horz_reference_frame and dem_vert_reference_frame must both be specified or "
            "able to be derived from a DEM file."
        )

    return dem_horz_reference_frame, dem_vert_reference_frame


def get_polygon(
    polygon_bbox_or_dem_fname: typing.Union[shapely.geometry, list, tuple, str],
) -> shapely.geometry.Polygon:
    """From a filename, a shapely geometry, or a bounding box, return a shapely polygon object."""
    if type(polygon_bbox_or_dem_fname) in (
        shapely.geometry.Polygon,
        shapely.geometry.MultiPolygon,
    ):
        return polygon_bbox_or_dem_fname
    elif (
        type(polygon_bbox_or_dem_fname) is list
        or type(polygon_bbox_or_dem_fname) is tuple
    ):
        if len(polygon_bbox_or_dem_fname) == 4:
            bbox = polygon_bbox_or_dem_fname
            return shapely.geometry.box(bbox[0], bbox[2], bbox[1], bbox[3])
        elif (
            len(polygon_bbox_or_dem_fname) > 4
            and len(polygon_bbox_or_dem_fname) % 2 == 0
        ):
            return shapely.geometry.Polygon(polygon_bbox_or_dem_fname)
        else:
            raise TypeError(
                "polygon_bbox_or_dem_fname must be a 4-value list of (xmin, xmax, ymin, ymax) or a greater "
                "length list/tuple defining a points of a polygon outline."
            )
    elif type(polygon_bbox_or_dem_fname) is str:
        if os.path.exists(polygon_bbox_or_dem_fname):
            bbox = rasterio.open(polygon_bbox_or_dem_fname).bounds
            return shapely.geometry.box(*bbox)
        else:
            raise FileNotFoundError(
                f"If polygon_bbox_or_fname is a string, it must point to a filename that exists. "
                f"Did not find {polygon_bbox_or_dem_fname}."
            )
    else:
        raise TypeError(
            "polygon_bbox_or_dem_fname must be either a 4-item bounding box, a list of coordinates "
            "defining a polygon, a shapely geometry Polygon, or a filename."
        )


def _filter_points_against_dem(
    df: pandas.DataFrame,
    polygon_bbox_or_dem_fname: typing.Union[shapely.geometry, list, tuple, str],
    dem_crs: pyproj.CRS,
    xy_cols: typing.Union[list, tuple] = ("x", "y"),
) -> pandas.DataFrame:
    """Filter the dataframe of points against the DEM outline or user-defined polygon in its native coordinates."""
    print("Converting to GeoDataFrame and subsetting photons...", end=" ", flush=True)
    gdf = geopandas.GeoDataFrame(
        df,
        geometry=geopandas.points_from_xy(df[xy_cols[0]], df[xy_cols[1]]),
        crs=dem_crs,
    )

    df_subset = df[gdf.intersects(get_polygon(polygon_bbox_or_dem_fname))]
    print("Done.", flush=True)

    return df_subset


def get_wgs84_bounding_box(
    polygon_bbox_or_dem_fname: typing.Union[shapely.geometry, list, tuple, str],
    dem_horz_reference_frame: typing.Union[str, pyproj.CRS, None] = None,
) -> tuple:
    """From a filename, a shapely geometry, or a bounding box, return a 4-value list of (xmin,xmax,ymin,ymax) in
    WGS84 (ITRF 2014) coordinates appropriate for querying NSIDC.

    Parameters:
        polygon_bbox_or_dem_fname: A shapely.geometry.Polygon object, or bounding-box is a 4-value list/tuple
            of (xmin,xmax,ymin,ymax). Coordinates should be in the reference frame outlined in dem_horz_reference_frame.
            Alternately, can also be the name of a DEM file from which to derive the bounding box.
        dem_horz_reference_frame: The horizontal reference frame of the DEM. Default None.
            If None, attempt to derive the horizontal reference frame from the DEM file if a filename is provided in
            the parameter polygon_bbox_or_dem_fname. This is None and no filename is provided, throws a ValueError.

    Raises:
        ValueError: if dem_horz_reference_frame is None, and no filename is provided in the
            parameter polygon_bbox_or_dem_fname.
        TypeError: If either parameter is of an unhandled type.

    Returns:
        A 4-value list of (xmin,xmax,ymin,ymax) in WGS84 (EPSG: 4326) coordinates appropriate for querying NSIDC.
    """
    polygon = None
    if type(polygon_bbox_or_dem_fname) is shapely.geometry.Polygon:
        polygon = shapely.Polygon(polygon_bbox_or_dem_fname.exterior.coords[:])

        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame, "horz"
        )

    elif (
        type(polygon_bbox_or_dem_fname) is list
        or type(polygon_bbox_or_dem_fname) is tuple
    ):
        if len(polygon_bbox_or_dem_fname) == 4:
            bbox = polygon_bbox_or_dem_fname
            # Convert the bounds from (xmin, xmax, ymin, ymax) to (xmin, ymin, xmax, ymax) work with shapely.
            bbox = [bbox[0], bbox[2], bbox[1], bbox[3]]
            polygon = shapely.geometry.box(*bbox)
        elif (
            len(polygon_bbox_or_dem_fname) > 4
            and len(polygon_bbox_or_dem_fname) % 2 == 0
        ):
            polygon = shapely.geometry.Polygon(polygon_bbox_or_dem_fname)
        else:
            raise TypeError(
                "polygon_bbox_or_dem_fname must be a 4-value list of (xmin, xmax, ymin, ymax) or a greater "
                "length list/tuple defining a points of a polygon outline."
            )

        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame, "horz"
        )

    elif type(polygon_bbox_or_dem_fname) is str:
        if os.path.exists(polygon_bbox_or_dem_fname):
            if dem_horz_reference_frame is None:
                dem_horz_reference_frame = get_dem_reference_frame_from_file(
                    polygon_bbox_or_dem_fname, "horz"
                )
            else:
                dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
                    dem_horz_reference_frame, "horz"
                )

            bbox = rasterio.open(polygon_bbox_or_dem_fname).bounds
            polygon = shapely.geometry.box(*bbox)

        else:
            raise FileNotFoundError(
                f"If polygon_bbox_or_fname is a string, it must point to a filename that exists. "
                f"Did not find {polygon_bbox_or_dem_fname}."
            )

    if dem_horz_reference_frame is None:
        raise ValueError("dem_horz_reference_frame not defined.")

    # These should both the true if we got here.
    assert type(polygon) is shapely.geometry.Polygon
    assert type(dem_horz_reference_frame) is pyproj.CRS
    assert not dem_horz_reference_frame.is_compound

    wgs84_crs = pyproj.CRS.from_user_input("EPSG:4326")

    # This DEM is already in WGS84, just return the bounding box of the polygon in (xmin,xmax,ymin,ymax) format.
    if dem_horz_reference_frame.equals(wgs84_crs):
        return (
            polygon.bounds[0],
            polygon.bounds[2],
            polygon.bounds[1],
            polygon.bounds[3],
        )

    # Otherwise, translate the polygon to WGS84 and return the bounding box of the polygon in (xmin,xmax,ymin,ymax) format.
    transformer = pyproj.Transformer.from_crs(
        dem_horz_reference_frame, wgs84_crs, always_xy=True
    )
    polygon_wgs84 = shapely.geometry.Polygon(
        shell=transformer.itransform(polygon.exterior.coords[:])
    )

    return (
        polygon_wgs84.bounds[0],
        polygon_wgs84.bounds[2],
        polygon_wgs84.bounds[1],
        polygon_wgs84.bounds[3],
    )


def get_dem_reference_frame_from_file(
    dem_fname: str, vert_horz_or_both: str = "both"
) -> typing.Union[pyproj.CRS, tuple, None]:
    """Get the reference frame of the DEM.

    Parameters:
        dem_fname: The name of the DEM file.
        vert_horz_or_both: Whether to return the vertical or horizontal reference frame. Will accept any value that
            starts in "v", "h", or "b" to mean "vertical", "horizontal", or "both", respectively. Default "both".

    Raises:
        ValueError: if vert_horz_or_both does not begin with one of "v", "h", or "b".
        FileNotFoundError: if the DEM file does not exist or has no crs attached.

    Returns:
        The vertical or horizontal reference frame of the DEM as a pyproj.CRS, or None if no reference frame could be found.
        If "vert_horz_or_both" is "both", 2-value a tuple of the vertical and horizontal reference frames is returned.
    """
    if not os.path.exists(dem_fname):
        raise FileNotFoundError(f"DEM file {dem_fname} does not exist.")
    dem_ds = rasterio.open(dem_fname)

    if dem_ds.crs is None:
        dem_ds_str = ""
    else:
        dem_ds_str = dem_ds.crs

    return get_dem_reference_frame_from_user_input(dem_ds_str, vert_horz_or_both)


def get_dem_reference_frame_from_user_input(
    crs: typing.Union[pyproj.CRS, rasterio.crs.CRS, str, int, None],
    vert_horz_or_both: str = "both",
) -> typing.Union[pyproj.CRS, tuple, None]:
    """Get the horizontal reference frame from an input string.

    Parameters:
        crs: The CRS string or object. If None, this will return None.
        vert_horz_or_both: Whether to return the vertical or horizontal reference frame. Will accept any value that
            starts in "v", "h", or "b" to mean "vertical", "horizontal", or "both", respectively. Default "both".

    Raises:
        ValueError: if vert_horz_or_both doesn't begin with one of "v", "h", or "b".

    Returns:
        The vertical or horizontal reference frame of the DEM as a pyproj.CRS, or None if no reference frame could be found.
        If "vert_horz_or_both" is "both", 2-value a tuple of the vertical and horizontal reference frames is returned.
    """
    if crs is None or crs == "":
        crs_obj = None
    elif isinstance(crs, rasterio.crs.CRS) or isinstance(crs, pyproj.CRS):
        crs_obj = pyproj.CRS(crs)
    else:
        crs_obj = pyproj.CRS.from_user_input(crs)

    if crs_obj is None:
        horz, vert = None, None
    elif crs_obj.is_compound:
        horz, vert = crs_obj.sub_crs_list
    elif len(crs_obj.axis_info) == 3:
        # If it's a 3D axis it's not "compound" but has 2 horz axes and a vert axis (3 total) which counts here.
        horz, vert = crs_obj, crs_obj
    elif crs_obj.is_vertical:
        horz, vert = None, crs_obj
    else:
        horz, vert = crs_obj, None

    # Get the lower-case first non-space letter from the vert_horz_or_both input string.
    choice_letter = vert_horz_or_both.strip().lower()[0]
    if choice_letter == "b":
        retval = horz, vert
    elif choice_letter == "h":
        retval = horz
    elif choice_letter == "v":
        retval = vert
    else:
        raise ValueError(
            f"Uknown choice '{vert_horz_or_both}' for 'vert_horz_or_both'. Should begin with 'h', 'v', or 'b'"
        )

    return retval


def _process_input_date_str(date_str: typing.Union[str, int]) -> str:
    """Process a date string and return a 'YYYY-MM-DD' string.

    Input date string can be anything that can be parsed by dateparser.parse().
    """
    try:
        if len(str(date_str)) == 8 and 19000000 <= int(date_str) <= 21000000:
            return datetime.datetime.strptime(str(date_str), "%Y%m%d").strftime(
                "%Y-%m-%d"
            )
    except ValueError:
        pass

    return dateparser.parse(str(date_str)).strftime("%Y-%m-%d")


def delta_time_to_yyyymmdd(
    delta_time: typing.Union[float, pandas.Series, numpy.ndarray],
    suggestion: typing.Union[str, int, None] = None,
    validate_suggestion_closeness: bool = True,
) -> typing.Union[int, numpy.ndarray]:
    """Get a datetime string from an icesat2 photon delta_time.

    Parameters:
        delta_time : float, pandas.Series, or numpy.ndarray
            Number of seconds since Jan 1 2018 00:00:00Z (ICESat-2 epoch)
        suggestion : str, int, or None, optional
            A suggested YYYYMMDD date near the expected result to speed up computation.
            Default is None, which uses slower but more accurate datetime calculations.
        validate_suggestion_closeness : bool, optional
            Whether to validate that all delta_time values are within +/- 1 day of the suggestion.
            Only used if suggestion is not None. Default is True.

    Raises:
        ValueError: If validate_suggestion_closeness is True and any delta_time values
            are more than 1 day from the suggested date.

    Returns:
        int or numpy.ndarray
            If input is float, returns integer YYYYMMDD date.
            If input is pandas.Series or numpy.ndarray, returns numpy array of YYYYMMDD dates.
    """
    if suggestion is None or type(delta_time) is float:
        icesat2_epoch = dateparser.parse("1 Jan 2018 00:00:00")

        if type(delta_time) is float:
            return int(
                (icesat2_epoch + datetime.timedelta(seconds=delta_time)).strftime(
                    "%Y%m%d"
                )
            )

        if type(delta_time) is pandas.Series:
            delta_time = delta_time.values

        vfunc = numpy.vectorize(
            lambda x: int(
                (icesat2_epoch + datetime.timedelta(seconds=x)).strftime("%Y%m%d")
            )
        )
        return vfunc(delta_time)

    # If we're given a suggestion that is within +/- 1 day of all values, we can do this with a much-faster vector comparison
    else:
        suggestion_dt = datetime.datetime.strptime(
            _process_input_date_str(suggestion), "%Y-%m-%d"
        )
        suggestion_minus_1_day = suggestion_dt - datetime.timedelta(days=1)
        suggestion_plus_1_day = suggestion_dt + datetime.timedelta(days=1)

        suggestion_int = int(suggestion_dt.strftime("%Y%m%d"))
        suggestion_minus_1_day_int = int(suggestion_minus_1_day.strftime("%Y%m%d"))
        suggestion_plus_1_day_int = int(suggestion_plus_1_day.strftime("%Y%m%d"))

        suggestion_delta_time = yyyymmdd_to_delta_time(suggestion_int)
        suggestion_plus_1_day_delta_time = yyyymmdd_to_delta_time(
            suggestion_plus_1_day_int
        )

        if validate_suggestion_closeness:
            suggestion_minus_1_day_delta_time = yyyymmdd_to_delta_time(
                suggestion_minus_1_day_int
            )
            suggestion_plus_2_days_delta_time = yyyymmdd_to_delta_time(
                int((suggestion_dt + datetime.timedelta(days=2)).strftime("%Y%m%d"))
            )
            if (
                numpy.count_nonzero(delta_time < suggestion_minus_1_day_delta_time) > 0
                or numpy.count_nonzero(delta_time >= suggestion_plus_2_days_delta_time)
                > 0
            ):
                raise ValueError(
                    f"delta_time values are too far from the given yyyymmdd suggestion of {suggestion}."
                )

        day_vals = numpy.zeros((len(delta_time),), dtype=int)
        # Any delta_times less than the suggested date must be the day before
        day_vals[delta_time < suggestion_delta_time] = suggestion_minus_1_day_int
        # Any delta_times greater than one day beyond the suggested date must be the day after
        day_vals[delta_time >= suggestion_plus_1_day_delta_time] = (
            suggestion_plus_1_day_int
        )
        # All other dates must be the suggested date.
        day_vals[day_vals == 0] = suggestion_int

        return day_vals


def yyyymmdd_to_delta_time(yyyymmdd: typing.Union[int, str]) -> float:
    """Get a an icesat2 photon delta_time from a datetime string."""
    return (
        datetime.datetime.strptime(str(yyyymmdd), "%Y%m%d")
        - dateparser.parse("1 Jan 2018 00:00:00")
    ).total_seconds()


def _get_classifications_str(
    classifications: typing.Union[str, list, tuple, numpy.ndarray],
) -> str:
    """Given a list of photon classifications codes, return a string separated by forward-slashes, compatible with dlim.

    Parameters:
        classifications: A string or list of strings of photon classifications codes.

    Returns:
        A string of classifications codes separated by forward-slashes.
    """
    if type(classifications) is str:
        # Remove spaces, brackets, parentheses. Replace commas with forward-slashes.
        return (
            classifications.replace(" ", "")
            .replace(",", "/")
            .replace("(", "")
            .replace(")", "")
            .replace("[", "")
            .replace("]", "")
        )

    else:
        return "/".join([str(int(c)) for c in classifications])


def export_as_vector(gdf: geopandas.GeoDataFrame, outfile: str):
    """Export a GeoDataFrame as a shapefile or geopackage."""
    if os.path.splitext(outfile)[-1] in (".gpkg", ".shp"):
        gdf.to_file(outfile)
    elif os.path.splitext(outfile)[-1] in (".blosc", ".blosc2"):
        utils.pickle_blosc.write(gdf, outfile)

    print(
        outfile,
        f"written with {len(gdf)}",
        f"{'photons' if isinstance(gdf.geometry.iloc[0], shapely.geometry.Point) else 'lines'}.",
    )


def export_lines(
    points_gdf_or_fname: typing.Union[geopandas.GeoDataFrame, str],
    outfile: str,
    tolerance=0.00001,
):
    """Take a points file produced from the ICESat-2 photon data and export a simplified set of lines that follows
    the path of each laser.

    Export as a vector file."""
    if isinstance(points_gdf_or_fname, str):
        print("Reading", os.path.basename(points_gdf_or_fname), end="...", flush=True)
        if os.path.splitext(points_gdf_or_fname)[-1].lower() in (".shp", ".gpkg"):
            points_gdf = geopandas.read_file(points_gdf_or_fname)
        elif os.path.splitext(points_gdf_or_fname)[-1].lower() in (".blosc", ".blosc2"):
            points_gdf = utils.pickle_blosc.read(points_gdf_or_fname)
        else:
            raise ValueError(f"Unrecognized file extension in {points_gdf_or_fname}.")

        print(" Done.", flush=True)
    else:
        assert isinstance(points_gdf_or_fname, geopandas.GeoDataFrame)
        points_gdf = points_gdf_or_fname

    # Group by the points by each unique laser ID, sort them by y-coordinate, and export as LineStrings into a new GDF.
    lines_gdf = points_gdf.groupby("unique_laser_id")["geometry"].apply(
        lambda x: shapely.geometry.LineString(
            sorted(x.tolist(), key=lambda p: p.coords[0][1])
        ).simplify(tolerance=tolerance)
    )

    export_as_vector(lines_gdf, outfile)


# def make_icesat2_requests(bbox: typing.Union[list, tuple],
#                           download_directory: str,
#                           subset_data: bool = True,
#                           use_previous_if_matching: bool = True,
#                           ) -> dict:
#     """For a given bounding box, request all the ICESat-2 data from NASA's Harmony service to perform an IVERT job.
#
#     This will entail requesting the ATL03, ATL08, and ATL24 datasets (if existing) for each granule within the bounding box.
#     Bounding boxes are 6-tuples including (xmin, xmax, ymin, ymax, tmin, tmax).
#
#     Return a dictionary with the dataset short_name as the key ("ATL03", e.g.) and the Harmony status JSON dict as the value.
#     A dictionary of dictionaries, as it were."""
#
#     json_responses = {}
#     requests_csv = icesat2_requests.ICESat2RequestsCSV()
#
#     for short_name in ("ATL03", "ATL08", "ATL24"):
#         # ATL03 granules go in "cmr". ATL08 & ATL24 granules go in "cmr/cmr".
#         # This is hard-coded, keep ICESat-2 data in the EPSG:4326+3855 datum. It will be converted ad-hoc to DEM coordinates
#         # when doing an IVERT validation.
#
#         if use_previous_if_matching:
#             previous_request_json = requests_csv.find_matching_request(short_name,
#                                                                        bbox,
#                                                                        only_unexpired=True,
#                                                                        return_rows=False)
#
#             if previous_request_json is not None:
#                 json_responses[short_name] = previous_request_json
#                 continue
#
#         # Create a CUDEM region object.
#         region = regions.Region().from_list(list(bbox[0:4]))
#
#         # Get date strings "yyyy-mm-dd:00:00:00" from each tmin, tmax.
#         start_datestr = _process_input_date_str(bbox[4])
#         end_datestr = _process_input_date_str(bbox[5])
#
#         is2 = earthdata.IceSat2(src_region=region,
#                                 outdir=os.path.join(download_directory, "cmr") \
#                                     if (short_name == "ATL03") \
#                                     else os.path.join(download_directory, "cmr", "cmr"),
#                                 time_start=start_datestr,
#                                 time_end=end_datestr,
#                                 subset=subset_data,
#                                 short_name=short_name,
#                                 version="006") # Keep it v006 for now until all the v007 files have been populated.
#
#         json = is2.harmony_make_request()
#         json_responses[short_name] = json
#
# def download_icesat2_when_ready(json_requests: dict,
#                                 overwrite: bool = False):
#     "Given a dictionary of short_name: harmony_json pairs (where 'short_name' is something like 'ATL03'), download all the data
#
# def search_previous_harmony_requests(bbox,
#                                      short_name: string = "ATL03",
#                                      subset_data: bool = True) -> typing.Union(dict, None):
#     """If a previous Harmony request matches the one we're making, return the job JSON dict for that previous job.
#
#     Will only return it if the data for the previous job hasn't expired."""
#     # TODO: Implement
#     return None


def define_and_parse_args():
    """Define and parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Query ICESat-2 data.")
    parser.add_argument("outfile", help="The name of the output file.")
    parser.add_argument(
        "-reg",
        "--region",
        dest="region",
        type=str,
        default=None,
        help="The region to query (in output coordinates). A comma-separated string (no spaces) "
        "of xmin,xmax,ymin,ymax, or a longer string of x1,y1,x2,y2,x3,y3,etc coordinates "
        "defining a polygon.",
    )
    parser.add_argument(
        "-rast",
        "--raster",
        dest="raster",
        default=None,
        help="The name of a raster file to use to define the polygon extent. "
        "One of 'region' or 'input_raster' must be set.",
    )
    parser.add_argument(
        "-crs",
        "--crs",
        dest="crs",
        default=None,
        help="The coordinate reference system of the polygon or raster. Default: if 'raster' is "
        "set, get the horizontal reference frame from the raster. If used, will override any crs "
        "defined in the raster."
        "At least one of --raster or --crs must be set.",
    )
    parser.add_argument(
        "-vd",
        "--vdatum",
        dest="vdatum",
        default=None,
        help="The vertical datum of the polygon or raster. Default: if --raster is set, attempt"
        "to get the horizontal datum from the input raster. Else, if --crs is set and is a "
        "compound or 3D coordinate system, get the vertical datum from that. "
        "If neither of these conditions is true, --vdatum must be set.",
    )
    parser.add_argument(
        "-s",
        "--start_date",
        dest="start_date",
        default="a year ago midnight",
        help="The start date in a format that python.dateparser can read. Default is 'a year ago midnight'.",
    )
    parser.add_argument(
        "-e",
        "--end_date",
        default="midnight today",
        help="The end date in a format that python.dateparser can read. Default is 'midnight today'.",
    )
    parser.add_argument(
        "-cols",
        "--other_columns",
        dest="other_columns",
        default="/gtx/heights/delta_time",
        help="A comma-separated list of extra ICESat-2 ATL03 data columns to include in the output. "
        "Default is only necessary columns, which includes 'delta_time' along with x,y,z.",
    )
    parser.add_argument(
        "-conf",
        "--confidence_levels",
        dest="conf_levels",
        default="4",
        help="A comma-separated (or /-separated) list of confidence levels to include in the output. "
        "Values can be 1, 2, 3, 4. Default is 4 (highest-confidence photons only).",
    )
    parser.add_argument(
        "-cls",
        "--classes",
        dest="classes",
        default="1,2,3,7,40,41",
        help="A comma-(or /)-separated list of classifications to include in the output. "
        "Values can be -1, 0, 1, 2, 3, 6, 7, 40, 41. Run 'dlim --modules icesat2' for a full list."
        "Default is 1,2,3,7,40,41.",
    )
    parser.add_argument(
        "-nobuild",
        "--no_buildings",
        dest="buildings",
        default=True,
        action="store_false",
        help="Do not include building classifications in the output. Default: Use Bing to classify "
        "buildings as class_code 7.",
    )
    parser.add_argument(
        "-nobathy",
        "--no_bathymetry",
        dest="bathymetry",
        default=True,
        action="store_false",
        help="Do not include bathymetry classifications in the output. Default: "
        "Use the ATL24 data product to classify bathymetry as class_code 4 (bathy floor) and 5 (bathy surface).",
    )
    parser.add_argument(
        "-l",
        "--lines",
        dest="lines",
        default=None,
        help="The name of a vector file where simplified lines following the path of each laser will be exported.",
    )

    args = parser.parse_args()
    return args


def main():

    args = define_and_parse_args()
    if args.region is None and args.raster is None:
        print("One of --region or --raster must be set.", file=sys.stderr)
        sys.exit(1)
    if args.region is not None and args.raster is not None:
        print("Only one of --region or --raster can be set.", file=sys.stderr)
        sys.exit(1)
    if not args.crs and not args.raster:
        print("At least one of --crs or --raster must be set.", file=sys.stderr)
        sys.exit(1)

    if args.region:
        args.region = [float(x) for x in args.region.split(",")]

    if args.other_columns:
        args.other_columns = ast.literal_eval(args.other_columns)

    gdf = get_photon_dataframe(
        args.raster if args.raster else args.region,
        dem_horz_reference_frame=args.crs,
        dem_vert_reference_frame=args.vdatum,
        start_date=args.start_date,
        end_date=args.end_date,
        other_columns=args.other_columns,
        classify_bathymetry=args.bathymetry,
        classify_buildings=args.buildings,
        classifications_to_keep=args.classes,
        conf_levels_to_keep=args.conf_levels,
    )

    export_as_vector(gdf, args.outfile)

    if args.lines:
        export_lines(gdf, args.lines)


if __name__ == "__main__":
    # main()
    download_granules(
        (-124.71, -124.68, 48.36, 48.44, 20240301, 20250301),
        output_directory=os.path.expanduser("~/.ivert/icesat2/test_granules"),
        download_directory=os.path.expanduser("~/.ivert/cache/test"),
        other_columns={},
        # other_columns={"/gtx/heights/delta_time":"delta_time"},
    )
