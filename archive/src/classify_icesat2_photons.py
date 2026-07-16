# -*- coding: utf-8 -*-

"""
retrieve_land_photons.py

Code for isolating land photons from ATL08, 06, and 03 datasets, for land-elevation validation.
Author: Mike MacFerrin
Created: 2021-06-10
"""

import geopandas
import numpy
import pandas
import os
import shapely
import sys

import atl_granules
import utils.configfile

my_config = utils.configfile.Config()

# TODO: Implement this in parallel.


def classify_photon_data_multiple_granules(
    gid_list,
    beam=None,
    return_type=numpy.ndarray,
    bounding_box=None,
    bbox_converter=None,
    parallelize=True,
    verbose=True,
):
    if parallelize:
        # TODO: call get_photon_data_multiple_granules_parallel and return that array, once that's implemented.
        pass

    array_list = [None] * len(gid_list)

    # THIS TAKES A WHILE.
    # TODO: Parallelize this. Would be easy to do. Put in separate dataframe picklefiles and save them separately,
    #    then read and concatenate them at the end.
    if verbose:
        print("Reading and linking ICESat-2 granules for land & canopy photon data.")

    # if verbose:
    #     progress_bar.ProgressBar(0, len(gid_list), suffix = "{}/{}".format(0,len(gid_list)))
    for i, gid in enumerate(gid_list):
        if verbose:
            print("{0}/{1}".format(i + 1, len(gid_list)), gid, end=" ")

        granule_h5_name_base, ext = os.path.splitext(gid)
        granule_h5_name = (
            os.path.join(
                os.path.dirname(granule_h5_name_base),
                os.path.split(granule_h5_name_base)[1].replace("ATL08", "ATL03"),
            )
            + "_photons"
            + ext
        )

        array_list[i] = classify_photon_data(
            gid,
            beam=beam,
            return_type=return_type,
            output_db=granule_h5_name,
            bounding_box=bounding_box,
            bbox_converter=bbox_converter,
        )  # , surface=surface)

        # Print number of photons. Use comma separators in the formatting.
        if verbose:
            print(
                "{:,}".format(array_list[i].shape[0]),
                "photons in bounding box.",
                "{:,}".format(numpy.count_nonzero(array_list[i]["class_code"] == 1)),
                "ground photons.",
            )
        # if verbose:
        #     progress_bar.ProgressBar(i+1, len(gid_list), suffix = "{}/{}".format(i+1,len(gid_list)))

    if type(array_list[0]) == numpy.ndarray:
        master_array = numpy.concatenate(array_list)
        if verbose:
            print("{:,}".format(len(master_array)), "total photons.")
            print(
                "{:,}".format(numpy.count_nonzero(master_array["class_code"] == 1)),
                "ground photons.",
            )
    elif type(array_list[0]) == pandas.DataFrame:
        master_array = pandas.concat(array_list)
        if verbose:
            print("{:,}".format(len(master_array)), "total photons.")
            print(
                "{:,}".format(
                    master_array.class_code[master_array["class_code"] == 1].count()
                ),
                "ground photons.",
            )

    return master_array


def classify_photon_data(
    granule_id,
    beam=None,
    return_type=numpy.ndarray,
    output_db=None,
    overwrite=False,
    bounding_box=None,
    bbox_converter=None,
    verbose=True,
):
    """Get the data on individual photons for a given granule ID.

    granule_id can be an ATL08 or 03 id. This function will convert automatically.

    return_type can be:
        - numpy.ndarray,
        - pandas.DataFrame, or
        - geopandas.GeoDataFrame.

    These are in order of speed. The ndarray is fastest, the geodataframe is by
    far the slowest because they haven't figured out a say to actually vectorize
    making those shapely geometries yet.

    If bounding_box is set (xmin, ymin, xmax, ymax), then filter out points
    to only those within the bounding box. Discard the rest.

    Surfaces:
        'ellipsoid' - WGS84 Ellipsoid Heights
        'geoid'     - EGM2008 Geoid Heights
        'mean_tide' - Mean-tide height (slightly different than geoid)
    """

    # tic = timeit.default_timer()

    # Strip off any paths
    # granule_id = os.path.split(granule_id)[1]
    # Strip off the extension
    # base, ext = os.path.splitext(granule_id)
    # if ext.lower() in (".h5", ".hdf5"):
    #     granule_id = base

    # Get the lowercase version of the surface.
    # surface = surface.lower()

    # Depending which granule ID we were given (ATL03, or -08), get the name for the other one.
    if granule_id.find("ATL03") > -1:
        atl03_gid = granule_id
        atl08_gid = granule_id.replace("ATL03", "ATL08")
    elif granule_id.find("ATL08") > -1:
        atl08_gid = granule_id
        atl03_gid = granule_id.replace("ATL08", "ATL03")
    else:
        raise FileNotFoundError(
            f"Granule ID does not appear to be an ATL03 or ATL08 granule: {granule_id}"
        )

    if (
        (output_db is not None)
        and (not overwrite)
        and (
            os.path.exists(output_db)
            or (
                os.path.exists(
                    os.path.splitext(output_db)[0]
                    + (
                        ".feather"
                        if (os.path.splitext(output_db)[1].lower() == ".h5")
                        else ".h5"
                    )
                )
            )
        )
    ):
        base, ext = os.path.splitext(output_db)
        ext = ext.lower()
        if os.path.exists(base + ".feather"):
            df = pandas.read_feather(base + ".feather")
        else:
            df = pandas.read_hdf(base + ".h5", key="icesat2", mode="r")

    else:
        # Get both data granules.
        atl03 = atl_granules.ATL03_granule(atl03_gid)
        atl08 = atl_granules.ATL08_granule(atl08_gid)

        # Get the beam, or list of beams.
        if beam is None:
            beams = ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]
        elif type(beam) in (list, tuple):
            beams = beam
        elif type(beam) == str:
            beams = [beam]
        else:
            raise TypeError(
                "Unhandled variables type for parameter 'beam': {0}".format(type(beam))
            )

        # Get the sizes of each array. This is quick (doesn't take long to read the shape).
        # This allows us to create an empty array of the correct size without the memory
        # & processing penalty of concatenating large arrays later.
        N = 0
        h3 = atl03._open_dataset()
        for beam in beams:
            try:
                N += h3[f"/{beam}/heights/lat_ph"].shape[0]
            except KeyError:
                continue

        # Generate a single array, with dtype fields for all our data bits.
        if bbox_converter:
            # If a bounding box conversion method is given, provide two extra fields
            # 'proj_x' and 'proj_y' for the projected x- and y-coordinates.
            out_array = numpy.empty(
                (N,),
                dtype=numpy.dtype(
                    [
                        ("latitude", numpy.float64),
                        ("longitude", numpy.float64),
                        ("h_ellipsoid", numpy.float32),
                        ("h_geoid", numpy.float32),
                        ("h_meantide", numpy.float32),
                        ("delta_time", numpy.float64),
                        ("beam", numpy.uint8),
                        ("granule_id1", numpy.int64),
                        ("granule_id2", numpy.int64),
                        ("dist_x", numpy.float64),
                        ("class_code", numpy.int8),
                        ("conf_land", numpy.int8),
                        ("conf_ocean", numpy.int8),
                        ("conf_sea_ice", numpy.int8),
                        ("conf_land_ice", numpy.int8),
                        ("conf_inland_water", numpy.int8),
                        ("quality_ph", numpy.int8),
                        ("proj_x", numpy.float64),
                        ("proj_y", numpy.float64),
                    ]
                ),
            )
        else:
            # else, leave those extra fields out.
            out_array = numpy.empty(
                (N,),
                dtype=numpy.dtype(
                    [
                        ("latitude", numpy.float64),
                        ("longitude", numpy.float64),
                        ("h_ellipsoid", numpy.float32),
                        ("h_geoid", numpy.float32),
                        ("h_meantide", numpy.float32),
                        ("delta_time", numpy.float64),
                        ("beam", numpy.uint8),
                        ("granule_id1", numpy.int64),
                        ("granule_id2", numpy.int64),
                        ("dist_x", numpy.float64),
                        ("class_code", numpy.int8),
                        ("conf_land", numpy.int8),
                        ("conf_ocean", numpy.int8),
                        ("conf_sea_ice", numpy.int8),
                        ("conf_land_ice", numpy.int8),
                        ("conf_inland_water", numpy.int8),
                        ("quality_ph", numpy.int8),
                    ]
                ),
            )

        # Convert the granule_id to 2x integers for storage in the database.
        gid1, gid2 = atl03.granule_id_to_intx2()

        out_array["granule_id1"] = gid1
        out_array["granule_id2"] = gid2

        N_so_far = 0
        get_data_kwargs = {"warn_if_not_present": True, "max_warnings": 0}
        for i, beam in enumerate(beams):
            # atl08_classed_pc_flag, \
            # atl08_ph_segment_id,   \
            # atl08_classed_pc_indx = atl08.get_ATL08_photon_data(beam=beam)

            # classed_pc_flag: Land Vegetation ATBD classification flag for each photon as either noise,
            #                  ground, canopy, or canopy top
            atl08_classed_pc_flag = atl08.get_data(
                "/[gtx]/signal_photons/classed_pc_flag", beam=beam, **get_data_kwargs
            )
            # ph_segment_id: Segment ID of photons tracing back to specific 20m segment_id on ATL03.
            #                The unique identifier for tracing each ATL08 signal photon to the photon
            #                on ATL03 is the segment_id, orbit, and classed_pc_indx. The unique identifier for
            #                tracing each ATL08 signal photon to the corresponding photon record
            #                on ATL03 is the segment_id, orbit, cycle, and classed_pc_indx.
            #                Orbit and cycle intervals for the granule are found in the /ancillary_data.
            #                The timestamp of each orbit transition is found in the /orbit_info group.
            atl08_ph_segment_id = atl08.get_data(
                "/[gtx]/signal_photons/ph_segment_id", beam=beam, **get_data_kwargs
            )
            # classed_pc_indx: Index (1-based) of the ATL08 classified signal photon from the
            #                  start of the ATL03 geolocation segment specified on the ATL08 product at the
            #                  photon rate in the corresponding parameter, ph_segment_id. This
            #                  index traces back to specific photon within a 20m segment_id on
            #                  ATL03. The unique identifier for tracing each ATL08 signal photon to the
            #                  corresponding photon record on ATL03 is the segment_id, orbit, cycle, and classed_pc_indx.
            #                  Orbit and cycle intervals for the granule are found in the
            #                  /ancillary_data. The timestamp of each orbit transition is found in the /orbit_info group.
            atl08_classed_pc_indx = atl08.get_data(
                "/[gtx]/signal_photons/classed_pc_indx", beam=beam, **get_data_kwargs
            )

            # segment_ph_cnt: Number of photons in a given along-track segment. In the case of no
            #                 photons within the segment (segment_ph_cnt=0), most other
            #                 parameters are filled with invalid or best-estimate values. Maintaining
            #                 geolocation segments with no photons allows for the geolocation
            #                 segment arrays to be directly aligned across the gtx groups.
            atl03_segment_ph_cnt = atl03.get_data(
                "/[gtx]/geolocation/segment_ph_cnt", beam=beam, **get_data_kwargs
            )
            # segment_id: A 7 digit number identifiying the along-track geolocation segment
            #             number. These are sequential, starting with 1 for the first segment
            #             after an ascending equatorial crossing node.
            atl03_segment_id = atl03.get_data(
                "/[gtx]/geolocation/segment_id", beam=beam, **get_data_kwargs
            )

            # Create a dictionary with (segment_id --> index into ATL03 photons) lookup pairs, for the starting photon of each segment
            atl03_segment_indices = numpy.concatenate(
                ([0], numpy.cumsum(atl03_segment_ph_cnt)[:-1])
            )
            atl03_segment_index_dict = dict(
                zip(atl03_segment_id, atl03_segment_indices)
            )

            dict_success = False
            while not dict_success:
                try:
                    atl08_ph_segment_indx = numpy.array(
                        list(
                            map(
                                (lambda pid: atl03_segment_index_dict[pid]),
                                atl08_ph_segment_id,
                            )
                        )
                    )
                except KeyError as e:
                    # One of the atl08_ph_segment_id entries does not exist in the atl03 granule, which
                    # causes problems here. Eliminate it from the list and try again.
                    problematic_id = e.args[0]
                    good_atl08_mask = atl08_ph_segment_id != problematic_id
                    atl08_classed_pc_flag = atl08_classed_pc_flag[good_atl08_mask]
                    atl08_ph_segment_id = atl08_ph_segment_id[good_atl08_mask]
                    atl08_classed_pc_indx = atl08_classed_pc_indx[good_atl08_mask]
                    # Then, try the loop again.
                    continue

                dict_success = True

            # atl08_ph_index: is a list of indices into the ATL03 array of photons
            # atl08_classed_pc_index is 1-based, so must subtract 1. Also, make sure it's a numpy int array.
            atl08_ph_index = numpy.array(
                atl08_ph_segment_indx + atl08_classed_pc_indx - 1, dtype=int
            )

            # Get lat/lon/height
            atl03_lat = atl03.get_data(
                "/[gtx]/heights/lat_ph", beam=beam, **get_data_kwargs
            )
            this_N = atl03_lat.shape[0]

            # If no data in this beam, move along to the next.
            if this_N == 0:
                continue

            # Define the slice of the array that we're filling in with this beam.
            slc = slice(N_so_far, N_so_far + this_N)

            out_array["latitude"][slc] = atl03_lat
            out_array["longitude"][slc] = atl03.get_data(
                "/[gtx]/heights/lon_ph", beam=beam, **get_data_kwargs
            )
            out_array["delta_time"][slc] = atl03.get_data(
                "/[gtx]/heights/delta_time", beam=beam, **get_data_kwargs
            )

            # Compute the total along-track distances.
            atl03_dist_ph_along = atl03.get_data(
                "/[gtx]/heights/dist_ph_along", beam=beam, **get_data_kwargs
            )
            atl03_segment_dist_x = atl03.get_data(
                "[gtx]/geolocation/segment_dist_x", beam=beam, **get_data_kwargs
            )
            atl03_segment_dist_dict = dict(zip(atl03_segment_id, atl03_segment_dist_x))

            # Determine where in the array each segment index needs to look.
            atl03_ph_segment_ids = atl03_segment_id[
                numpy.searchsorted(atl03_segment_indices, numpy.arange(0.5, this_N, 1))
                - 1
            ]
            atl03_ph_segment_dist_x = numpy.array(
                list(
                    map(
                        (lambda pid: atl03_segment_dist_dict[pid]), atl03_ph_segment_ids
                    )
                )
            )
            out_array["dist_x"][slc] = atl03_ph_segment_dist_x + atl03_dist_ph_along

            # Get the ellipsoid, geoid, and meantide heights from the arrays.

            h_ellipsoid = atl03.get_data(
                "/[gtx]/heights/h_ph", beam=beam, **get_data_kwargs
            )
            # if surface == 'ellipsoid':
            out_array["h_ellipsoid"][slc] = h_ellipsoid

            # elif surface in ('geoid', 'mean_tide'):
            h_geoid = atl03.get_data(
                "/[gtx]/geophys_corr/geoid", beam=beam, **get_data_kwargs
            )

            atl03_h_geoid_dict = dict(zip(atl03_segment_id, h_geoid))

            # Get the mean-tide of each photon based on the
            atl03_ph_h_geoid = numpy.array(
                list(map((lambda pid: atl03_h_geoid_dict[pid]), atl03_ph_segment_ids))
            )

            # if surface == 'mean_tide':
            h_meantide = atl03.get_data(
                "/[gtx]/geophys_corr/geoid_free2mean", beam=beam, **get_data_kwargs
            )

            atl03_h_meantide_dict = dict(zip(atl03_segment_id, h_meantide))

            atl03_ph_h_meantide = numpy.array(
                list(
                    map((lambda pid: atl03_h_meantide_dict[pid]), atl03_ph_segment_ids)
                )
            )

            out_array["h_geoid"][slc] = h_ellipsoid - atl03_ph_h_geoid
            out_array["h_meantide"][slc] = h_ellipsoid - (
                atl03_ph_h_geoid + atl03_ph_h_meantide
            )

            # Fill in data quality metrics.
            out_array["quality_ph"][slc] = atl03.get_data(
                "/[gtx]/heights/quality_ph", beam=beam, **get_data_kwargs
            )
            signal_conf_ph = atl03.get_data(
                "/[gtx]/heights/signal_conf_ph", beam=beam, **get_data_kwargs
            )
            out_array["conf_land"][slc] = signal_conf_ph[:, 0]
            out_array["conf_ocean"][slc] = signal_conf_ph[:, 1]
            out_array["conf_sea_ice"][slc] = signal_conf_ph[:, 2]
            out_array["conf_land_ice"][slc] = signal_conf_ph[:, 3]
            out_array["conf_inland_water"][slc] = signal_conf_ph[:, 4]

            # else:
            #     raise ValueError("Unknown surface type '{}'".format(surface))

            # Type codes:
            # -1 : uncoded
            #  0 : noise
            #  1 : ground
            #  2 : canopy
            #  3 : top of canopy
            # First, make them all default to -1
            out_array["class_code"][slc] = numpy.zeros((this_N,), dtype=numpy.int8) - 1
            # Then, fill in the remaining numbers with the ATL08 codes
            out_array["class_code"][slc][atl08_ph_index] = atl08_classed_pc_flag

            out_array["beam"][slc] = atl03.beam_name_to_int(beam)

            N_so_far += this_N

        if return_type == numpy.ndarray:
            # The fastest option, just return the numpy array.
            # quickest for appending later too.
            df = out_array

        elif return_type == pandas.DataFrame:
            # Medium-speed option, a dataframe but without a geometry associated.
            df = pandas.DataFrame(
                dict(
                    latitude=out_array["latitude"],
                    longitude=out_array["longitude"],
                    h_ellipsoid=out_array["h_ellipsoid"],
                    h_geoid=out_array["h_geoid"],
                    h_meantide=out_array["h_meantide"],
                    delta_time=out_array["delta_time"],
                    beam=out_array["beam"],
                    granule_id1=out_array["granule_id1"],
                    granule_id2=out_array["granule_id2"],
                    dist_x=out_array["dist_x"],
                    class_code=out_array["class_code"],
                    conf_land=out_array["conf_land"],
                    conf_ocean=out_array["conf_ocean"],
                    conf_sea_ice=out_array["conf_sea_ice"],
                    conf_land_ice=out_array["conf_land_ice"],
                    conf_inland_water=out_array["conf_inland_water"],
                    quality_ph=out_array["quality_ph"],
                )
            )
        elif return_type == geopandas.GeoDataFrame:
            # By far the slowest, by about a factor of 10-20x, creating all those Point() geometries serially sucks CPU cycles.
            # But, if you want a geospatially-enabled dataframe, here you go.
            df = geopandas.GeoDataFrame(
                dict(
                    latitude=out_array["latitude"],
                    longitude=out_array["longitude"],
                    h_ellipsoid=out_array["h_ellipsoid"],
                    h_geoid=out_array["h_geoid"],
                    h_meantide=out_array["h_meantide"],
                    delta_time=out_array["delta_time"],
                    beam=out_array["beam"],
                    granule_id1=out_array["granule_id1"],
                    granule_id2=out_array["granule_id2"],
                    dist_x=out_array["dist_x"],
                    class_code=out_array["class_code"],
                    conf_land=out_array["conf_land"],
                    conf_ocean=out_array["conf_ocean"],
                    conf_sea_ice=out_array["conf_sea_ice"],
                    conf_land_ice=out_array["conf_land_ice"],
                    conf_inland_water=out_array["conf_inland_water"],
                    quality_ph=out_array["quality_ph"],
                ),
                # geometry=list(map(shapely.geometry.Point, a_lons, a_lats)),
                geometry=[
                    shapely.geometry.Point(p)
                    for p in zip(out_array["latitude"], out_array["longitude"])
                ],
                crs="EPSG:4326",
            )

        else:
            raise ValueError("Unknown return type: {}".format(str(return_type)))

    if (output_db is not None) and (overwrite or not os.path.exists(output_db)):
        base, ext = os.path.splitext(output_db)
        ext = ext.lower()
        if ext == ".h5":
            df.to_hdf(output_db, "icesat2", mode="w", complib="zlib", complevel=3)
        else:
            assert ext == ".feather"
            df.to_feather(
                output_db,
                compression=my_config.feather_database_compress_algorithm,
                compression_level=my_config.feather_database_compress_level,
            )
        if verbose:
            print(
                "\n" + os.path.split(output_db)[1],
                "written with {0:,} total photons.".format(len(df)),
            )

    # If we ware given a bounding box, only return photons that lie within that
    # bounding box. This cuts down the total data volume significantly, efficiently.
    # NOTE: This has only been tested on the numpy.ndarray and pandas.DataFrame types.
    if bounding_box:
        xmin, ymin, xmax, ymax = bounding_box
        if bbox_converter:
            # Convert all the lat/lon coordinates into the new projection.
            points = bbox_converter.TransformPoints(
                list(zip(df["longitude"], df["latitude"]))
            )
            p_x = numpy.array([p[0] for p in points])
            p_y = numpy.array([p[1] for p in points])
            df["proj_x"] = p_x
            df["proj_y"] = p_y
            # Then submset the bounding box by the projected coordinates.
            df_subset = df[
                (df["proj_x"] >= xmin)
                & (df["proj_x"] < xmax)
                & (df["proj_y"] >= ymin)
                & (df["proj_y"] < ymax)
            ]
        else:
            # Else, just subset the bounding box by the latitude & longitude
            df_subset = df[
                (df["longitude"] >= xmin)
                & (df["longitude"] < xmax)
                & (df["latitude"] >= ymin)
                & (df["latitude"] < ymax)
            ]

        df = df_subset

    # Code to see how long this runs (comment out later)
    # toc1 = timeit.default_timer()
    # print(toc1 - tic, "seconds.")
    # import sys
    # print(sys.getsizeof(df), "bytes.")

    # Close the datasets (which closes the HDF5 files).
    try:
        atl03.close()
        atl08.close()
    # Some of the branches above didn't define atl03 and atl08 yet. In that case, ignore closing them.
    except NameError:
        pass

    return df


def save_photon_data_from_directory_or_list_of_granules(
    dirname_or_list_of_granules,
    photon_db="photons.h5",
    bounding_box=None,
    bbox_converter=None,
    beam=None,
    verbose=True,
):
    """Do everything that's in get_photon_data_multiple_granules(), just get the files from
    a given directory, and save it to a given photon HDF5 file.

    'dirname_or_list_of_granules' can either be:
        - The name of a directory where all the ATLXX granule .h5 files sit.
        - A lit of either the ATL03 or the ATL08 granules in them. (The code will look for the others). All the .h5 files should ideally be in the same directory."""
    if type(dirname_or_list_of_granules) == str and os.path.isdir(
        dirname_or_list_of_granules
    ):
        fnames = os.listdir(dirname_or_list_of_granules)
        h5_fnames = [
            fname for fname in fnames if os.path.splitext(fname)[1].lower() == ".h5"
        ]
        atl03_fnames = sorted(
            [
                os.path.join(dirname_or_list_of_granules, fname)
                for fname in h5_fnames
                if fname.upper().find("ATL03") >= 0
            ]
        )
        atl08_fnames = sorted(
            [
                os.path.join(dirname_or_list_of_granules, fname)
                for fname in h5_fnames
                if fname.upper().find("ATL08") >= 0
            ]
        )

    elif type(dirname_or_list_of_granules) in (list, tuple):
        atl03_fnames = [
            os.path.join(
                os.path.dirname(fname),
                os.path.split(fname)[1].replace("ATL08", "ATL03"),
            )
            for fname in dirname_or_list_of_granules
        ]
        # Turn into a set to just get unique values, then back to a sorted list of unique files that exist.
        atl03_fnames = sorted(
            list(set([fname for fname in atl03_fnames if os.path.exists(fname)]))
        )

        atl08_fnames = [
            os.path.join(
                os.path.dirname(fname),
                os.path.split(fname)[1].replace("ATL03", "ATL08"),
            )
            for fname in dirname_or_list_of_granules
        ]
        # Turn into a set to just get unique values, then back to a sorted list of unique files that exist.
        atl08_fnames = sorted(
            list(set([fname for fname in atl08_fnames if os.path.exists(fname)]))
        )
    else:
        raise ValueError(
            "Uknown value for 'dirname_or_list_of_granules':",
            dirname_or_list_of_granules,
            "\nMust either point to an existing directory, or be a list or tuple of granule names.",
        )

    # Find the granules that are common between ATL03 and ATL08 (Should be all of them):
    common_granule_ids = []
    for atl03_gid in atl03_fnames:
        if (
            os.path.join(
                os.path.dirname(atl03_gid),
                os.path.split(atl03_gid)[1].replace("ATL03", "ATL08"),
            )
            in atl08_fnames
        ):
            common_granule_ids.append(atl03_gid)

    # Append the dirname
    # common_granule_ids = sorted([os.path.join(dirname, fname) for fname in common_granule_ids])

    dataframe = classify_photon_data_multiple_granules(
        common_granule_ids,
        beam=beam,
        return_type=pandas.DataFrame,
        bounding_box=bounding_box,
        bbox_converter=bbox_converter,
        parallelize=True,
        verbose=verbose,
    )

    # If a path is given for the photon_h5 file, keep it. Otherwise, put it in the
    # current working directory.
    if os.path.split(photon_db)[0] == "":
        photon_db = os.path.join(os.getcwd(), photon_db)

    base, ext = os.path.splitext(photon_db)
    ext = ext.lower()
    if ext == ".h5":
        dataframe.to_hdf(photon_db, "icesat2", complib="zlib", complevel=3, mode="w")
    else:
        assert ext == ".feather"
        dataframe.to_feather(
            photon_db,
            compression=my_config.feather_database_compress_algorithm,
            compression_level=my_config.feather_database_compress_level,
        )
    if verbose:
        print(photon_db, "written.")

    return dataframe


def read_or_create_granule_photons(
    granule_path, output_db=None, overwrite=False, verbose=True
):
    if output_db is None:
        output_db_h5 = os.path.splitext(granule_path)[0] + "_photons.h5"
        output_db_feather = os.path.splitext(output_db)[0] + ".feather"
        # Default to the feather file format, for quicker read/writes.
        output_db = output_db_feather
    else:
        base, ext = os.path.splitext(output_db)
        ext = ext.lower()
        if ext == ".h5":
            output_db_h5 = output_db
            output_db_feather = base + ".feather"
        else:
            assert ext == ".feather"
            output_db_h5 = base + ".h5"
            output_db_feather = output_db

    if os.path.exists(output_db_feather):
        if verbose:
            print("Reading", os.path.split(output_db_feather)[1] + "...", end="")
        dataframe = pandas.read_feather(output_db_feather)
        if verbose:
            print(" Done.")
    elif os.path.exists(output_db_h5):
        if verbose:
            print("Reading", os.path.split(output_db_h5)[1] + "...", end="")
        dataframe = pandas.read_hdf(output_db_h5, key="icesat2", mode="r")
        if verbose:
            print(" Done.")

    else:
        dataframe = save_granule_ground_photons(
            granule_path, output_db=output_db, overwrite=overwrite, verbose=verbose
        )

    return dataframe


def save_granule_ground_photons(
    granule_path, output_db=None, delete_granules=True, overwrite=False, verbose=True
):
    """For one granule, save all the ground and canopy photons to a file.

    No bounding-box subsetting, just save it all.
    """
    if output_db is None:
        output_db = os.path.splitext(granule_path)[0] + "_photons.feather"

    base, ext = os.path.splitext(output_db)
    ext = ext.lower()
    # if ext == ".h5":
    #     output_h5 = output_db
    #     output_feather = base + ".feather"
    # else:
    #     assert ext == ".feather"
    #     output_h5 = base + ".h5"
    #     output_feather = output_db

    # If the granule is the ATL08 granule, name it after the ATL03 granule instead.
    if (os.path.split(output_db)[1].find("ATL03") < 0) and (
        os.path.split(output_db)[1].find("ATL08") >= 0
    ):
        output_db = os.path.join(
            os.path.dirname(output_db),
            os.path.split(output_db)[1].replace("ATL08", "ATL03"),
        )

    if os.path.exists(output_db):
        if overwrite:
            os.remove(output_db)
        else:
            if verbose:
                print(output_db, "already exists.")
            return

    if verbose:
        print(os.path.split(output_db)[1], end="... ")
        # Make sure it actually gets printed to the screen.
        sys.stdout.flush()

    dataframe = classify_photon_data(
        granule_path,
        beam=None,
        return_type=pandas.DataFrame,
        bounding_box=None,
        bbox_converter=None,
    )

    # Subset only ground and canopy photons.
    # -1 = unclassified
    #  0 = noise/atmosphere
    #  1 = ground
    #  2 = canopy
    #  3 = canopy top
    # Save only 1,2,3 photons.
    dataframe = dataframe.loc[
        dataframe.class_code.between(1, 3, inclusive="both")
    ].reset_index()

    if ext == ".h5":
        dataframe.to_hdf(output_db, "icesat2", complib="zlib", complevel=3, mode="w")
    else:
        assert ext == ".feather"
        dataframe.to_feather(
            output_db,
            compression=my_config.feather_database_compress_algorithm,
            compression_level=my_config.feather_database_compress_level,
        )

    if verbose:
        print("Done.")

    if delete_granules:
        atl03_granule = os.path.join(
            os.path.dirname(granule_path),
            os.path.split(granule_path)[1].replace("ATL08", "ATL03"),
        )
        atl08_granule = os.path.join(
            os.path.dirname(granule_path),
            os.path.split(granule_path)[1].replace("ATL03", "ATL08"),
        )
        if os.path.exists(atl03_granule):
            os.remove(atl03_granule)
            if verbose:
                print(atl03_granule, "deleted.")

        if os.path.exists(atl08_granule):
            os.remove(atl08_granule)
            if verbose:
                print(atl08_granule, "deleted.")

    return dataframe


def define_and_parse_args():
    # TODO: Fill in command-line options to be able to classify some photon granules on the fly.
    pass


if __name__ == "__main__":
    print(
        "Nothing in __main__ executable here. This is a set of utilities primarily used by ./validate_dem.py"
    )

    # output_shapefile_of_granules("../data/temp/ne_dems_copernicus/NE_photons.shp", "../data/temp/ne_dems_copernicus")
    # df = save_photon_data_from_directory("../data/temp/ne_dems_copernicus", "NE_photons.h5", bounding_box=[-72,41,-70,43], verbose=True)

    # df = save_photon_data_from_directory("../data/temp/everest_full", "everest_photons.h5", bounding_box=[86,27,87,28], verbose=True)
    # output_shapefile_of_granules("../data/temp/everest_full/everest_tracks.shp", "../data/temp/everest_full")

    # save_photon_data_from_directory("../data/great_barrier_reef_2", "gbr2_photons.h5", bounding_box = [142,-16,147,-12], verbose=True)
    # output_shapefile_of_granules("../data/great_barrier_reef_2/great_barrier_reef_2_orbits.shp", "../data/great_barrier_reef_2/", bbox=[142,-16,147,-12])
    # pass
    # df = get_photon_data("ATL03_20200102112805_01030606_004_01.h5", beam=None, return_type=numpy.ndarray) #geopandas.GeoDataFrame)
    # print(df)
