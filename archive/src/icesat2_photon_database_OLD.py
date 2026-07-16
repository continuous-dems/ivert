# -*- coding: utf-8 -*-

"""Code to manage spatial databases of classified land/veg icesat-2 photons
for memory-efficient handling of ICESat-2 data."""

import os
import pyproj
import geopandas
import pandas
import re
import numexpr
import numpy
from osgeo import osr, gdal
import shapely.geometry
import shutil
import tables
import time
import itertools
import pyarrow

#####################################
# Suppress the annoying pandas.FutureWarning warnings caused by library version conflicts.
# It doesn't affect my code and will be resolved in future releases of pandas.
# For now, just suppress the warning.
import warnings

warnings.filterwarnings("ignore", message=".*pandas.Int64Index is deprecated*")
#####################################

# import nsidc_download
import s3
import utils.configfile
import utils.progress_bar
import utils.sizeof_format
import utils.pickle_blosc


class ICESat2_Database:
    """A database to manage ICESat-2 photon clouds for land-surface validations.
    The database is a set of a tiles, managed spatially by a GeoPackage object,
    with each tile being an HDF5 database of all land & canopy photons classified
    from ICESat-2 granules that overlap each tile."""

    def __init__(self, ivert_config=None):
        """tile_resolutin_deg should be some even fraction of 1. I.e. 1, or 0.5, or 0.25, or 0.1, etc.

        The current geodatabase consists of 0.25 degree tiles, 417,760 of them covering the planet's land surface."""
        if ivert_config is None:
            self.ivert_config = utils.configfile.Config()
        else:
            self.ivert_config = ivert_config
        self.gpkg_fname = self.ivert_config.icesat2_photon_geopackage
        # Same file, just pickled using blosc2 compression in the utils/pickle_blosc.py script.
        self.gpkg_fname_compressed = os.path.splitext(self.gpkg_fname)[0] + ".blosc2"
        self.tiles_directory = self.ivert_config.icesat2_photon_tiles_directory
        self.granules_directory = self.ivert_config.icesat2_granules_directory

        self.gdf = None  # The actual geodataframe object.
        self.tile_resolution_deg = 0.25
        self.crs = pyproj.CRS.from_epsg(4326)
        self.s3_manager = None

    def get_gdf(self, verbose=True):
        """Return self.gdf if exists, otherwise read self.gpkg_name, save it to self.gdf and return."""
        if self.gdf is None:
            if not os.path.exists(self.gpkg_fname) and not os.path.exists(
                self.gpkg_fname_compressed
            ):
                if self.ivert_config.is_aws:
                    s3_manager = self.get_s3_manager()
                    s3_geopackage = self.ivert_config.s3_photon_geopackage_key
                    s3_geopackage_compressed = (
                        os.path.splitext(s3_geopackage)[0] + ".blosc2"
                    )
                    if s3_manager.exists(
                        s3_geopackage_compressed, bucket_type="database"
                    ):
                        is_compressed = True
                        s3_file_to_fetch = s3_geopackage_compressed

                    elif s3_manager.exists(s3_geopackage, bucket_type="database"):
                        is_compressed = False
                        s3_file_to_fetch = s3_geopackage
                    else:
                        raise FileNotFoundError(
                            os.path.basename(self.gpkg_fname)
                            + " not found on disk nor in S3 bucket '{0}'".format(
                                s3.get_bucketname()
                            )
                        )

                    # If the local directory doesn't exist, create it.
                    # This may happen when on a brand-new S3 instance.
                    if not os.path.exists(os.path.dirname(self.gpkg_fname)):
                        os.makedirs(os.path.dirname(self.gpkg_fname))

                    s3_manager.download(
                        s3_file_to_fetch,
                        self.gpkg_fname_compressed
                        if is_compressed
                        else self.gpkg_fname,
                        delete_original=False,
                        fail_quietly=not verbose,
                    )

            if os.path.exists(self.gpkg_fname_compressed):
                print("Reading", os.path.basename(self.gpkg_fname_compressed))
                self.gdf = utils.pickle_blosc.read(self.gpkg_fname_compressed)
            elif os.path.exists(self.gpkg_fname):
                print("Reading", os.path.basename(self.gpkg_fname))
                self.gdf = geopandas.read_file(self.gpkg_fname, mode="r")
            else:
                raise FileNotFoundError(
                    "Could not located photon_tile_geopackage to read IVERT GeoDataFrame."
                )

        return self.gdf

    def get_s3_manager(self):
        if self.s3_manager is None:
            self.s3_manager = s3.S3Manager()

        return self.s3_manager

    def numrecords(self):
        return len(self.get_gdf(verbose=False))

    # def create_new_geopackage(self, populate_with_existing_tiles=True, verbose=True):
    #     """Create the geopackage that handles the photon database files of ICESat-2 data."""
    #     # Columns to have : filename, xmin, xmax, ymin, ymax, numphotons, numphotons_canopy, numphotons_ground, geometry
    #     # "numphotons", "numphotons_canopy", "numphotons_ground" are all set to zero at first. Are populated later as files are written.
    #     # "geometry" will be a square polygon bounded by xmin, xmax, ymin, ymax
    #
    #     # TODO: Get rid of the Copernicus data dependency for "where ICESat-2 data exists". We need to find another way
    #     # to do this, especially since we'll be grabbing bathymetry data using CShelph as well.
    #
    #     # Since we're interested in land-only, we will use the CopernicusDEM dataset
    #     # to determine where land tiles might be.
    #     copernicus_gdf = None # TODO: Read the default Copernicus dataframe here to get a list of 1* land tiles.
    #     # copernicus_gdf = Copernicus.source_dataset_CopernicusDEM().get_geodataframe(verbose=verbose)
    #     copernicus_fnames = [os.path.split(fn)[1] for fn in copernicus_gdf["filename"].tolist()]
    #     copernicus_bboxes = [self.get_bbox_from_copernicus_filename(fn) for fn in copernicus_fnames]
    #     copernicus_bboxes.extend(etopo.generate_empty_grids.get_azerbaijan_1deg_bboxes()) # TODO: Eliminate this dependency as well.
    #     # Skip all bounding boxes with a y-min of -90, since ICESat-2 only goes down to -89.
    #     # Don't need to worry about north pole, since the northernmost land bbox tops out at 84*N
    #     copernicus_bboxes = [bbox for bbox in copernicus_bboxes if bbox[1] > -90]
    #
    #     copernicus_bboxes = sorted(copernicus_bboxes)
    #
    #     # Subtract the epsilon just to make sure we don't accidentally add an extra box due to a rounding error
    #     tiles_to_degree_ratio = int(1/(self.tile_resolution_deg - 0.0000000001))
    #     N = len(copernicus_bboxes) * (tiles_to_degree_ratio)**2
    #
    #     tile_filenames = [None] * N
    #     tile_xmins = numpy.empty((N,), dtype=numpy.float32)
    #     tile_xmaxs = numpy.empty((N,), dtype=numpy.float32)
    #     tile_ymins = numpy.empty((N,), dtype=numpy.float32)
    #     tile_ymaxs = numpy.empty((N,), dtype=numpy.float32)
    #     tile_geometries = [None] * N
    #     # These fields are all initialized to zero. Will be filled in as files are created.
    #     tile_numphotons = numpy.zeros((N,), dtype=numpy.uint32)
    #     tile_numphotons_canopy = numpy.zeros((N,), dtype=numpy.uint32)
    #     tile_numphotons_ground = numpy.zeros((N,), dtype=numpy.uint32)
    #     tile_is_populated = numpy.zeros((N,), dtype=bool)
    #     # Loop through each copernicus bbox, get tile bboxes
    #     i_count = 0
    #
    #     if verbose:
    #         print("Creating", self.gpkg_fname, "...")
    #
    #     for cop_bbox in copernicus_bboxes:
    #
    #         bbox_xrange = numpy.arange(cop_bbox[0], cop_bbox[2]-0.0000000001, self.tile_resolution_deg)
    #         bbox_yrange = numpy.arange(cop_bbox[1], cop_bbox[3]-0.0000000001, self.tile_resolution_deg)
    #         assert len(bbox_xrange) == tiles_to_degree_ratio
    #         assert len(bbox_yrange) == tiles_to_degree_ratio
    #         for tile_xmin in bbox_xrange:
    #             tile_xmax = tile_xmin + self.tile_resolution_deg
    #             for tile_ymin in bbox_yrange:
    #                 tile_ymax = tile_ymin + self.tile_resolution_deg
    #                 tile_fname = os.path.join(self.tiles_directory, "photon_tile_{0:s}{1:05.2f}_{2:s}{3:06.2f}_{4:s}{5:05.2f}_{6:s}{7:06.2f}.h5".format(\
    #                                                         "S" if (tile_ymin < 0) else "N",
    #                                                         abs(tile_ymin),
    #                                                         "W" if (tile_xmin < 0) else "E",
    #                                                         abs(tile_xmin),
    #                                                         "S" if (tile_ymax < 0) else "N",
    #                                                         abs(tile_ymax),
    #                                                         "W" if (tile_xmax < 0) else "E",
    #                                                         abs(tile_xmax)))
    #
    #                 tile_polygon = shapely.geometry.Polygon([(tile_xmin,tile_ymin),
    #                                                          (tile_xmin,tile_ymax),
    #                                                          (tile_xmax,tile_ymax),
    #                                                          (tile_xmax,tile_ymin),
    #                                                          (tile_xmin,tile_ymin)])
    #
    #                 tile_filenames[i_count] = tile_fname
    #                 tile_xmins[i_count] = tile_xmin
    #                 tile_xmaxs[i_count] = tile_xmax
    #                 tile_ymins[i_count] = tile_ymin
    #                 tile_ymaxs[i_count] = tile_ymax
    #                 tile_geometries[i_count] = tile_polygon
    #
    #                 i_count += 1
    #
    #     data_dict = {'filename': tile_filenames,
    #                  'xmin'    : tile_xmins,
    #                  'xmax'    : tile_xmaxs,
    #                  'ymin'    : tile_ymins,
    #                  'ymax'    : tile_ymaxs,
    #                  'numphotons'       : tile_numphotons,
    #                  'numphotons_canopy': tile_numphotons_canopy,
    #                  'numphotons_ground': tile_numphotons_ground,
    #                  'is_populated'     : tile_is_populated,
    #                  'geometry': tile_geometries
    #                  }
    #
    #     # Create the geodataframe
    #     self.gdf = geopandas.GeoDataFrame(data_dict, geometry='geometry', crs=self.crs)
    #     # Compute the spatial index, just to see if it saves it (don't think so but ???)
    #     # sindex = self.gdf.sindex
    #
    #     if verbose:
    #         print("gdf has", len(self.gdf), "tile bounding boxes.")
    #
    #     if populate_with_existing_tiles:
    #         # Read all the existing tiles, get the stats, put them in there.
    #         existing_mask = self.gdf['filename'].apply(os.path.exists, convert_dtype=False)
    #         # Get the subset of tiles where the file currently exists on disk.
    #         gdf_existing = self.gdf[existing_mask]
    #
    #         if verbose:
    #             print("Reading", len(gdf_existing), "existing tiles to populate database.")
    #
    #         for i,row in enumerate(gdf_existing.itertuples()):
    #             tile_df = pandas.read_hdf(row.filename, mode='r')
    #
    #             self.gdf.loc[row.Index, "numphotons"] = len(tile_df)
    #             self.gdf.loc[row.Index, "numphotons_canopy"] = numpy.count_nonzero(tile_df['class_code'].between(2,3,inclusive='both'))
    #             self.gdf.loc[row.Index, "numphotons_ground"] = numpy.count_nonzero(tile_df['class_code']==1)
    #             self.gdf.loc[row.Index, 'is_populated'] = True
    #
    #             if verbose:
    #                 utils.progress_bar.ProgressBar(i+1, len(gdf_existing), suffix="{0}/{1}".format(i+1, len(gdf_existing)))
    #
    #     # Save it out to an HDF file.
    #     self.save_geopackage(verbose=verbose)
    #
    #     return self.gdf

    # def get_bbox_from_copernicus_filename(self, filename):
    #     """From a CopernicusDEM filename, get the bbox [xmin,ymin,xmax,ymax].
    #
    #     We use this to generate where our icesat-2 photon tiles need to be.
    #
    #     This is specifically the Coperinicus 30m datasets (1 arc-second),
    #     which are 1-degree tiles."""
    #     # Copernicus tiles are all in Coperinicus_DSM_COG_10_NYY_00_EXXX_00_DEM.tif formats. Get the bboxes from there.
    #     lat_letter_regex_filter = r"(?<=Copernicus_DSM_COG_10_)[NS](?=\d{2}_00_)"
    #     lat_regex_filter = r"(?<=Copernicus_DSM_COG_10_[NS])\d{2}(?=_00_)"
    #     lon_letter_regex_filter = r"(?<=Copernicus_DSM_COG_10_[NS]\d{2}_00_)[EW](?=\d{3}_00_DEM\.tif)"
    #     lon_regex_filter = r"(?<=Copernicus_DSM_COG_10_[NS]\d{2}_00_[EW])\d{3}(?=_00_DEM\.tif)"
    #
    #     min_y = (1 if (re.search(lat_letter_regex_filter, filename).group() == "N") else -1) * \
    #             int(re.search(lat_regex_filter, filename).group())
    #     max_y = min_y + 1
    #
    #     min_x = (1 if (re.search(lon_letter_regex_filter, filename).group() == "E") else -1) * \
    #             int(re.search(lon_regex_filter, filename).group())
    #     max_x = min_x + 1
    #
    #     bbox = (min_x, min_y, max_x, max_y)
    #
    #     # print(filename, bbox)
    #     return bbox

    def add_datetime_fields_to_geopackage(
        self,
        start_date: str = "2021-01-01",
        end_date: str = "2021-12-31",
        default_version_num: int = 5,
        verbose: bool = True,
    ):
        """Add start_date and end_date fields to every entry in the geopackage. Also ATL03 version number.

        This allows us to add more data to the database later and search by date and/or ICESat-2 version.
        The first version of the database was built entirely from 2021 calendar year data and used ICESat-2 ATL03 v5.
        """
        gdf = self.get_gdf()
        gdf["start_date_YYYYMMDD"] = int(
            start_date.replace("-", "").replace("/", "").replace(".", "")
        )
        gdf["end_date_YYYYMMDD"] = int(
            end_date.replace("-", "").replace("/", "").replace(".", "")
        )
        gdf["atl03_version"] = default_version_num

        self.gdf = gdf

        # Save both the compressed and uncompressed versions.
        if verbose:
            print("Writing", os.path.basename(self.gpkg_fname_compressed))
        self.save_geopackage(gdf=gdf, use_tempfile=True, compress=True, verbose=verbose)
        if verbose:
            print("Writing", os.path.basename(self.gpkg_fname))
        self.save_geopackage(
            gdf=gdf, use_tempfile=True, compress=False, verbose=verbose
        )

        if verbose:
            print("Uploading", os.path.basename(self.gpkg_fname), "to s3 bucket.")
        self.upload_to_s3()

        print("Done.")
        return

    def upload_to_s3(self):
        """Upload the geopackage to the s3 bucket from the EC2 instance.

        If not in an AWS instance, do nothing."""
        if self.ivert_config.is_aws:
            self.get_s3_manager().upload(
                self.gpkg_fname,
                self.ivert_config.s3_photon_geopackage_key,
                bucket_type="database",
                delete_original=False,
                fail_quietly=False,
                recursive=False,
                include_md5=True,
                other_metadata=None,
            )
        return

    def fill_in_missing_tile_entries(
        self, delete_csvs=True, save_to_disk=True, verbose=True
    ):
        """Sometimes a photon_tile gets created and the _summary.csv file got deleted,
        but the database update wasn't saved. Loop through the existing photon tiles, fill
        in any missing entries in the database, and save it back out."""
        # First, let's ingest the CSV summary files in the directory.
        gdf = self.get_gdf(verbose=verbose)
        self.update_gpkg_with_csvfiles(
            gdf=gdf,
            use_tempfile=True,
            delete_when_finished=delete_csvs,
            save_to_disk=save_to_disk,
            verbose=verbose,
        )

        existing_tiles = [
            os.path.join(self.tiles_directory, fn)
            for fn in os.listdir(self.tiles_directory)
            if (re.search(r"\Aphoton_tile_[\w\.]+\.h5\Z", fn) != None)
        ]
        num_filled_in = 0
        for tilename in existing_tiles:
            tile_record = gdf.loc[gdf.filename == tilename]
            # If the tile exists and it says it's populated, move along.
            if tile_record["is_populated"].tolist()[0] == True:
                continue

            # Otherwise, let's get the data from the tile ane enter it.
            idx = tile_record.index.tolist()[0]
            try:
                tile_df = pandas.read_hdf(tilename, mode="r")
            except KeyboardInterrupt as e:
                raise e
            except Exception:
                # The tile might have an error if it was incompletely written before. If so, remove it.
                os.remove(tilename)
                summary_csv_name = os.path.splitext(tilename)[0] + "_summary.csv"
                if os.path.exists(summary_csv_name):
                    os.remove(summary_csv_name)

                continue

            gdf.loc[idx, "numphotons"] = len(tile_df)
            gdf.loc[idx, "numphotons_canopy"] = numpy.count_nonzero(
                tile_df["class_code"].between(2, 3, inclusive="both")
            )
            gdf.loc[idx, "numphotons_ground"] = numpy.count_nonzero(
                tile_df["class_code"] == 1
            )
            gdf.loc[idx, "is_populated"] = True
            num_filled_in += 1

        if num_filled_in > 0:
            if verbose:
                print(num_filled_in, "missing tiles entered into the database.")
            # Only re-save this to disk if we've actually updated anything. Otherwise,
            # it would be finished after the previous update_gpkg_with_csvfiles() call.
            if save_to_disk:
                self.save_geopackage(
                    gdf=gdf,
                    use_tempfile=True,
                    also_delete_redundant_csvs=False,
                    verbose=verbose,
                )

        if gdf is not self.gdf:
            self.gdf = gdf

        return gdf

    def save_geopackage(
        self,
        gdf=None,
        use_tempfile=False,
        compress=False,
        # also_delete_redundant_csvs=False,
        verbose=True,
    ):
        """After writing or altering data in the geo-dataframe, save it back out to disk.

        If gdf is None, use whatever object is in self.gdf.
        """
        if gdf is None:
            if self.gdf is None:
                raise ValueError(
                    "No geodataframe object available to ICESat2_Database.save_gpkg()."
                )
            gdf = self.gdf

        if gdf is not self.gdf:
            self.gdf = gdf

        file_to_write = self.gpkg_fname_compressed if compress else self.gpkg_fname

        base, ext = os.path.splitext(file_to_write)

        if use_tempfile:
            tempfile_name = base + "_TEMP" + ext
            if os.path.exists(tempfile_name):
                if verbose:
                    print(
                        tempfile_name,
                        "already exists.",
                        "\n\tExiting ICESat2_Database.save_geopackage(use_tempfile=True). Other processes may be writing to it.",
                        "\n\tIf this is in error, delete that file before running again.",
                    )
                return
            success = False
            while not success:
                try:
                    if ext == ".gpkg":
                        gdf.to_file(tempfile_name, layer="icesat2", driver="GPKG")
                    elif ext == ".blosc2":
                        utils.pickle_blosc.write(gdf, tempfile_name)
                    else:
                        raise NotImplementedError(
                            "Uknown file format for photon_tile_geopackage: {0}. Can accept .gpkg or .blosc2.".format(
                                os.path.basename(file_to_write)
                            )
                        )
                    os.remove(file_to_write)
                    shutil.move(tempfile_name, file_to_write)
                    success = True
                except Exception:
                    # Delete the tempfile, then re-raise the exception.
                    if os.path.exists(tempfile_name):
                        os.remove(tempfile_name)
                    if verbose:
                        print(
                            "Error occurred while writing",
                            os.path.basename(self.gpkg_fname)
                            + ". Waiting 30 seconds to retry...",
                        )
                    time.sleep(30)
        else:
            # Write the file.
            if ext == ".gpkg":
                gdf.to_file(file_to_write, layer="icesat2", driver="GPKG")
            elif ext == ".blosc2":
                utils.pickle_blosc.write(gdf, file_to_write)
            else:
                raise NotImplementedError(
                    "Uknown file format for photon_tile_geopackage: {0}. Can accept .gpkg or .blosc2.".format(
                        os.path.basename(file_to_write)
                    )
                )

        if verbose:
            print(os.path.basename(file_to_write), "written with", len(gdf), "entries.")
        return

        # if also_delete_redundant_csvs:
        #     self.delete_csvs_already_in_database(gdf = gdf,
        #                                          force_read_from_disk = False,
        #                                          verbose = verbose)

    def query_geopackage(
        self, polygon_or_bbox, return_whole_records=True, verbose=True
    ):
        """Return the photon database tile filenames that intersect the polygon in question.

        If return_whole_records is True, then return the entire dataset subset, including all the fields.
        If False, then just return the list of filenames.
        """
        # If we have a bounding box, query it using the bounds
        if (type(polygon_or_bbox) in (list, tuple)) and (len(polygon_or_bbox) == 4):
            polygon = shapely.geometry.box(*polygon_or_bbox, ccw=False)
        else:
            assert type(polygon_or_bbox) in (
                shapely.geometry.Polygon,
                shapely.geometry.polygon.Polygon,
                shapely.geometry.MultiPolygon,
                shapely.geometry.multipolygon.MultiPolygon,
            )
            polygon = polygon_or_bbox

        # Use the polygon intersection tool to find the intersection.
        gdf = self.get_gdf(verbose=verbose)

        # Subset the records that overlap but don't just "touch" (on an edge or corner).
        # gdf_subset = gdf[ gdf.intersects(polygon) & ~gdf.touches(polygon)]
        # First, use the spatial index to dramatically (and quickly) narrow down the overlapping tiles using bboxes.
        gdf_sub1 = gdf.loc[gdf.sindex.query(polygon)]
        # Then find all tiles in that subset that overlap but do not just "touch" on an edge or corner.
        gdf_subset = gdf_sub1[gdf_sub1.intersects(polygon) & ~gdf_sub1.touches(polygon)]

        if return_whole_records:
            return gdf_subset
        else:
            return gdf_subset["filename"].tolist()

    def get_photon_dataframe(
        self,
        polygon_or_bbox=None,
        build_tiles_if_nonexistent=False,
        good_photons_only=True,
        dem_fname=None,
        dem_fname_orig_backup=None,
        dem_epsg=None,
        verbose=True,
    ):
        """Given a polygon or bounding box, return the combined database of all
        the photons within the polygon or bounding box.

        Polygon is a shapely.geometry.Polygon object, or bounding-box is a 4-value list/tuple
        of (xmin,ymin,xmax,ymax). Coordinates should be in WGS84 (EPSG:4326) lat/lon. Any other
        coordinate system transformations should be done before being sent to this method.

        Return value:
            A pandas.DataFrame object containing the photon data of all tiles that overlap the
            given bounding-box.
        """
        df_tiles_subset = self.query_geopackage(
            polygon_or_bbox, return_whole_records=True
        )
        if verbose:
            print(
                len(df_tiles_subset.index),
                "ICESat-2 photon tiles overlap this polygon. Retrieving them.",
            )

        dataframes_list = [None] * len(df_tiles_subset.index)

        if dem_fname is not None:
            dem_ds = gdal.Open(dem_fname, gdal.GA_ReadOnly)
            assert dem_ds is not None
        else:
            dem_ds = None

        # For each tile, read the data into a dataframe.
        for i, (idx, df_row) in enumerate(df_tiles_subset.iterrows()):
            fname = df_row["filename"]
            if verbose:
                print(
                    "\t{0}/{1} Reading".format(i + 1, len(df_tiles_subset)),
                    os.path.splitext(os.path.basename(fname))[0],
                    "...",
                    end="",
                )

            tile_df = self.read_photon_tile(fname)

            if tile_df is not None and good_photons_only:
                # Filter out to keep only the highest-quality photons.
                # quality_ph == 0 ("nominal") and "conf_land" == 4 ("high") and/or "conf_land_ice" == 4 ("high")
                # Using photon_df.eval() is far more efficient for complex expressions than a boolean python expression.
                good_photon_mask = tile_df.eval(
                    "(quality_ph == 0) & ((conf_land == 4) | (conf_land_ice == 4)) & (class_code >= 1)"
                )
                new_tile_df = tile_df[good_photon_mask].copy()
                # Try to complete de-reference the previous dataframe to free up memory.
                del tile_df
                tile_df = new_tile_df

            if verbose:
                print("Done.")

            # If the file doesn't exist, create it and get the data.
            if tile_df is None and build_tiles_if_nonexistent:
                if verbose:
                    print(
                        "\t{0}/{1} Creating".format(i + 1, len(df_tiles_subset)),
                        os.path.split(fname)[1],
                        "...",
                    )
                tile_df = self.create_photon_tile(
                    df_row["geometry"],
                    fname,
                    overwrite=False,
                    write_stats=True,
                    verbose=verbose,
                )

            # If the DEM is not in WGS84, project the points into the DEM coordinate system.
            if tile_df is not None and dem_fname is not None:
                if len(tile_df) == 0:
                    tile_df["dem_x"] = 0.0
                    tile_df["dem_y"] = 0.0

                elif dem_epsg is not None and dem_epsg != 4326:
                    dem_proj_wkt = dem_ds.GetProjection()

                    if (
                        dem_proj_wkt is None
                        or len(dem_proj_wkt) == 0
                        and dem_fname_orig_backup is not None
                    ):
                        # If the primary file fails (maybe something got messed up in the vertical transformation),
                        # try to read the projection from the original DEM file.

                        dem_ds = gdal.Open(dem_fname_orig_backup, gdal.GA_ReadOnly)
                        dem_proj_wkt = dem_ds.GetProjection()
                        dem_ds = None

                    assert dem_proj_wkt is not None and len(dem_proj_wkt) > 0

                    icesat2_srs = osr.SpatialReference()
                    icesat2_srs.SetWellKnownGeogCS("EPSG:4326")
                    dem_srs = osr.SpatialReference(wkt=dem_proj_wkt)

                    is2_to_dem = osr.CoordinateTransformation(icesat2_srs, dem_srs)

                    lon_x = tile_df["longitude"]
                    lat_y = tile_df["latitude"]
                    latlon_array = numpy.array([lon_x, lat_y]).transpose()

                    points = numpy.array(is2_to_dem.TransformPoints(latlon_array))

                    tile_df["dem_x"] = points[:, 0]
                    tile_df["dem_y"] = points[:, 1]

                else:
                    tile_df["dem_x"] = tile_df["longitude"]
                    tile_df["dem_y"] = tile_df["latitude"]

                # Now subset only photons that are within the bounding box of the DEM.
                xstart, xstep, _, ystart, _, ystep = dem_ds.GetGeoTransform()
                dem_xsize = dem_ds.RasterXSize
                dem_ysize = dem_ds.RasterYSize
                xend = xstart + (xstep * dem_xsize)
                yend = ystart + (ystep * dem_ysize)
                ph_xcoords = tile_df["dem_x"]
                ph_ycoords = tile_df["dem_y"]

                # Clip to the bounding box.
                minx = min(xstart, xend)
                maxx = max(xstart, xend)
                miny = min(ystart, yend)
                maxy = max(ystart, yend)
                # Again, using a numexpr expression here is far more time-and-memory efficient than doing all these compound boolean
                # operations on the numpy arrays in a Python expression.
                ph_bbox_mask = numexpr.evaluate(
                    "(ph_xcoords >= minx) & "
                    "(ph_xcoords < maxx) & "
                    "(ph_ycoords > miny) & "
                    "(ph_ycoords <= maxy)",
                    local_dict={
                        "ph_xcoords": ph_xcoords,
                        "ph_ycoords": ph_ycoords,
                        "minx": minx,
                        "maxx": maxx,
                        "miny": miny,
                        "maxy": maxy,
                    },
                )

                # By creating a copy of the subset, it ensures the original is dereferenced and deleted from memory.
                tile_df = tile_df[ph_bbox_mask].copy()

            dataframes_list[i] = tile_df

        # Get rid of any dataframes where data wasn't read.
        dataframes_list = [df for df in dataframes_list if (df is not None)]

        # Concatenate the dataframes together.
        if len(dataframes_list) > 0:
            combined_df = pandas.concat(dataframes_list, ignore_index=True)
            return combined_df
        else:
            return None

    def read_empty_tile(self, verbose=True):
        """Sometimes the "create_photon_tile() function gets literally zero photon dataframes back
        from its quiery. In that case, just return a copy of an empty dataframe we've
        made and saved with all the correct fields but no data records.

        If the empty dataframe doesn't exist, just read a random dataframe (hoping
        one of those exists), empty it, and save it out to the
        ivert_config.icesat2_photon_empty_tile file."""
        if os.path.exists(self.ivert_config.icesat2_photon_empty_tile):
            empty_df = pandas.read_hdf(
                self.ivert_config.icesat2_photon_empty_tile, mode="r"
            )
        else:
            # If we can't find the empty tile, create it by gleaming off one of the other databases.
            # NOTE: This assumes at least one photon tile or one photon granul
            # database already exists in their respective folders. It might be a good idea down
            # the line to not rely upon this assumption. Maybe include the empty tile in the
            # git repository so we ensure it's there.
            list_of_files = [
                fn
                for fn in os.listdir(self.tiles_directory)
                if (re.search(r"\Aphoton_tile_[\w\.]+\.((h5)|(feather))\Z", fn) != None)
            ]
            if len(list_of_files) > 0:
                example_file = os.path.join(self.tiles_directory, list_of_files[0])
            else:
                list_of_files = [
                    fn
                    for fn in os.listdir(self.granules_directory)
                    if re.search(r"\AATL03_(\w)+_photons\.((h5)|(feather))\Z", fn)
                    != None
                ]
                if len(list_of_files) == 0:
                    raise FileNotFoundError(
                        "Could not find an existing photon tile or granule to use to create the file",
                        self.ivert_config.icesat2_photon_empty_tile,
                    )
                example_file = os.path.join(self.granules_directory, list_of_files[0])

            df = pandas.read_hdf(example_file, mode="r")
            # Empty out all the records and return the empty dataframe.
            empty_df = df[[False] * len(df)]
            empty_df.to_hdf(self.ivert_config.icesat2_photon_empty_tile, key="icesat2")
            if verbose:
                print(self.ivert_config.icesat2_photon_empty_tile, "written.")

        assert len(empty_df) == 0
        return empty_df

    def look_for_local_icesat2_photon_db(
        self, db_name, look_for_subset_at_longitude=None, lon_chunksize=2
    ):
        """Look through the local granules directory, as well as any alternate directories, to find ICESat-2 photon database granule files.
        Return the file location if it exists. Return None if not.

        If look_for_subset_at_longitude is set to a number, it will first look for a subset granule that fits that longitude."""

        # First, if we've opted to do this with the optional parameters, look for a smaller subset of the granule file. If it exists, just use that.
        if (look_for_subset_at_longitude is not None) and (lon_chunksize is not None):
            granule_sub_filename = self.give_granule_subset_name(
                db_name, look_for_subset_at_longitude, lon_deg_chunksize=lon_chunksize
            )
            if os.path.exists(granule_sub_filename):
                return granule_sub_filename

        # Otherwise, look for either a .h5 or .feather file in the granules directory.
        # If you can't find it in the main granules directory, look in the alternate directories.
        possible_exts = [".h5", ".feather"]
        possible_dirs = [self.granules_directory]  ## + self.alt_granules_directories

        basename, ext = os.path.splitext(os.path.basename(db_name))
        assert ext in possible_exts

        # Look in all the possible directories, using both possible extensions, for this photon database file.
        for dirname in possible_dirs:
            for ext_name in possible_exts:
                possible_fname = os.path.join(dirname, basename + ext_name)
                if os.path.exists(possible_fname):
                    return possible_fname

        # Otherwise, if we couldn't find a matching granule or subset-of-a-granule anywhere, just return None.
        return None

    # def create_photon_tile(self, bbox_polygon,
    #                              tilename,
    #                              date_range = ['2021-01-01', '2021-12-31'], # Calendar-year 2021 is the dates we're using for ETOPO. TODO: Change this to read from the Config file later.
    #                              overwrite = False,
    #                              write_stats = True,
    #                              verbose = True):
    #     """If a photon tile doesn't exist yet, download the necessary granules and create it."""
    #     # If the tile exists, either delete it (if overwrite=True) or read it
    #     # and return the datafarme (if overwrite=False)
    #     if type(bbox_polygon) == shapely.geometry.Polygon:
    #         # Get (xmin, ymin, xmax, ymax)
    #         bbox_bounds = bbox_polygon.bounds
    #     else:
    #         # If it's already a 4-length tuple, assume it's the bbox and go from there.
    #         assert type(bbox_polygon) in (list, tuple, numpy.ndarray) and len(bbox_polygon) == 4
    #         bbox_bounds = bbox_polygon
    #
    #     # Read the tile. If it doesn't exist, create it.
    #     if os.path.exists(tilename):
    #         if overwrite:
    #             os.remove(tilename)
    #         else:
    #             try:
    #                 # If overwrite=False and the file exists, read it and just return the dataframe.
    #                 return self.read_photon_tile(tilename)
    #             except KeyboardInterrupt as e:
    #                 # If we hit a keyboard interrupt while reading this, don't do
    #                 # anything, just re-raise the interrupt. I'm probably trying
    #                 # to kill the program halfway through working (just let me).
    #                 raise e
    #             except Exception:
    #                 # If the file is incompliete or somehow corrupted, delete it and we'll try this again.
    #                 if verbose:
    #                     print("Error encountered while attempting to read photon tile {0}. Removing it to re-create it.".format(os.path.split(tilename)[1]))
    #                 os.remove(tilename)
    #
    #     # Okay, so the tile doesn't exist. Query the icesat-2 photons files that overlap this bounding-box.
    #     # granule_names = nsidc_download.download_granules(short_name=["ATL03", "ATL08"],
    #     #                                                  region = bbox_bounds,
    #     #                                                  local_dir = self.granules_directory,
    #     #                                                  dates = date_range,
    #     #                                                  download_only_matching_granules = True,
    #     #                                                  query_only = True,
    #     #                                                  quiet = True)
    #
    #     atl03_granules = [fn for fn in granule_names if os.path.split(fn)[1].find("ATL03") > -1]
    #     atl08_granules = [fn for fn in granule_names if os.path.split(fn)[1].find("ATL08") > -1]
    #     assert len(atl03_granules) == len(atl08_granules)
    #
    #     # Generate the names of the _photon.h5 files from the ATL03 filenames.
    #     atl03_photon_db_filenames = [(base + "_photons" + ext) for (base, ext) in [os.path.splitext(fn) for fn in atl03_granules]]
    #
    #     # Then, see if the _photon.h5 databases exist for all these tiles. If they do, read them.
    #     photon_dfs = [None]*len(atl03_photon_db_filenames)
    #     # gdf = None
    #
    #     if verbose:
    #         print("Reading {0} _photons.h5 databases to generate {1}.".format(len(atl03_photon_db_filenames), os.path.split(tilename)[1]))
    #     for i,(photon_db,atl3,atl8) in enumerate(zip(atl03_photon_db_filenames, atl03_granules, atl08_granules)):
    #         df = None
    #
    #         # I am making is so the photon databases can be either .h5 or .feather database formats.
    #         # .h5 (saved flat)
    #         # base, ext = os.path.splitext(photon_db)
    #         # ext = ext.lower()
    #         # if ext == ".h5":
    #         #     photon_db_other = base + ".feather"
    #         # else:
    #         #     photon_db_other = base + ".h5"
    #
    #         # Generate a temporary empty textfile to indicate this file is currently being downloaded.
    #         # This helps prevent multiple processes from downloading the files all at the same time.
    #         # CAUTION: If this process is terminated or does not complete successfully and the _TEMP_DOWNLOADING.txt
    #         # file is not removed, this could cause a locking problem where some/all of the processes in subsequent runs are
    #         # waiting on some non-existent process to supposedly finish downloading. A locking problem.
    #         # I sort of pre-empted this somewhat by
    #         # deleting the file in the exception block, but if the code truly is killed instantly
    #         # (say, if the power suddenly goes out on your computer)
    #         # the exception block will not be reached and that will not work. This is a somewhat reliable solution
    #         # under normal operating conditions and handles most typical execution errors, but is not a 100% thread-safe locking solution.
    #         # A better solution would take more time to implement and this counts as "good enough for now, until the photon database is complete."
    #         downloading_fbase, downloading_fext = os.path.splitext(photon_db)
    #         downloading_fname = downloading_fbase + "_TEMP_DOWNLOADING.txt"
    #
    #         df_is_read = False
    #         photon_db_location = None
    #         while not df_is_read:
    #             # If the tile doesn't exist, get the granules needed for it. First, look for the photon database, either in .h5 or .feather format.
    #             if photon_db_location is None:
    #                 photon_db_location = self.look_for_local_icesat2_photon_db(photon_db, look_for_subset_at_longitude=None) #=(None if bbox_bounds[1] > -88 else bbox_bounds[0]))
    #                 # TODO: DELETE THIS LATER.
    #                 # FOR NOW, we know that if there exists no "subset datafraome" for this tile in this bounding box, then there are
    #                 # no photons in that bounding box for this tile. Don't bother reading it to check again.
    #                 # Just look for the pattern to see if this was a subset tile. If not, skip & move along.
    #                 # print(photon_db_location)
    #                 # if bbox_bounds[1] <= -88 and re.search("_[EW]((\d){3})_[EW]((\d){3})\.feather", photon_db_location) is None:
    #                 #     df = self.read_empty_tile()
    #
    #             if photon_db_location is None:
    #                 # If the granules don't exist, download them.
    #
    #                 # Check for existence of "_TEMP_DOWNLOADING.txt" file.
    #                 # Skip if this file already exists.
    #                 if os.path.exists(downloading_fname):
    #                     time.sleep(1)
    #                     continue
    #
    #                 if not os.path.exists(atl3) or not os.path.exists(atl8):
    #
    #                     # Create an emtpy textfile marking the existence of a process
    #                     # that is currently downloading the data for this granule.
    #                     with open(downloading_fname, 'w') as f:
    #                         f.close()
    #
    #                     # If either of the granules aren't there, download them from NSIDC
    #                     # Only granules that don't currently exist will be downloaded
    #                     try:
    #                         list_of_files = nsidc_download.download_granules(short_name=["ATL03", "ATL08"],
    #                                                                          region=bbox_bounds,
    #                                                                          local_dir = self.granules_directory,
    #                                                                          dates = date_range,
    #                                                                          download_only_matching_granules = False,
    #                                                                          query_only = False,
    #                                                                          fname_filter = os.path.split(atl3)[1][5:], # Get only the files with this granule ID number
    #                                                                          quiet = not verbose
    #                                                                          )
    #                     except Exception as e:
    #                         # The download_granules() method can return an exception,
    #                         # or raise one, either way, just assign it to the return
    #                         # variable to be handled immediately below.
    #                         list_of_files = e
    #
    #                     if isinstance(list_of_files, Exception):
    #                         # If the download script return an exception, or one is raised, delete the file saying that a download is happening.
    #                         if os.path.exists(downloading_fname):
    #                             os.remove(downloading_fname)
    #
    #                         if isinstance(list_of_files, KeyboardInterrupt):
    #                             # If the session ended because it was killed, then just exit the hell outta here. We're done.
    #                             raise list_of_files
    #                         else:
    #                             # Otherwise, go back to the top of the loop and try again.
    #                             continue
    #
    #                 # Create the photon database if it doesn't already exist. (And grab the datframe from it.)
    #                 df = classify_icesat2_photons.save_granule_ground_photons(atl3,
    #                                                                           output_db = photon_db,
    #                                                                           overwrite = False,
    #                                                                           verbose=verbose)
    #
    #                 # Remove the temp downloading file.
    #                 if os.path.exists(downloading_fname):
    #                     os.remove(downloading_fname)
    #
    #             else:
    #                 # Update the photon_db variable from its default location to wherever it actually sits on the various
    #                 # drives.
    #                 photon_db = photon_db_location
    #
    #             # print(i+1, len(atl03_photon_db_filenames), photon_db)
    #
    #             # At this point, the photon database should exist locally. So read it.
    #             # Then, subset within the bounding box.
    #             if df is None:
    #                 base, ext = os.path.splitext(photon_db)
    #                 try:
    #                     if ext == ".h5":
    #                         df = pandas.read_hdf(photon_db, mode='r')
    #                     else:
    #                         df = pandas.read_feather(photon_db)
    #                 except (AttributeError, tables.exceptions.HDF5ExtError):
    #                     print("===== ERROR: Photon database {0} corrupted. Will build anew. =====".format(os.path.basename(photon_db)))
    #                     print("Removing", photon_db)
    #                     # Remove the corrupted database.
    #                     os.remove(photon_db)
    #                     continue
    #                 except KeyboardInterrupt as e:
    #                     print("Process {} was reading".format(os.getpid()), photon_db, "in bbox ({0:0.2f},{1:0.2f},{2:0.2f},{3:0.2f})".format(*bbox_bounds))
    #                     raise e
    #                 # except FileNotFoundError:
    #                 #     # If the file is not found, try to find the other one. One of them should be in here.
    #                 #     if ext == ".h5":
    #                 #         df = pandas.read_feather(photon_db_other)
    #                 #     else:
    #                 #         df = pandas.read_hdf(photon_db_other, mode='r')
    #
    #             # Only bother subsetting the dataframe if there are any photons in it.
    #             if len(df.index) == 0:
    #                 df_subset = df
    #             else:
    #                 # Select only photons within the bounding box, that are land (class_code==1) or canopy (==2,3) photons
    #                 df_subset = df[df.longitude.between(bbox_bounds[0], bbox_bounds[2], inclusive="left") & \
    #                                df.latitude.between(bbox_bounds[1], bbox_bounds[3], inclusive="left") & \
    #                                df.class_code.between(1,3, inclusive="both")]
    #
    #             photon_dfs[i] = df_subset
    #
    #             df_is_read = True
    #
    #     # Now concatenate the databases.
    #     # If there are no files to concatenate, just read the empty database and return that.
    #     if len(photon_dfs) == 0:
    #         tile_df = self.read_empty_tile()
    #     else:
    #         tile_df = pandas.concat(photon_dfs, ignore_index=True)
    #     # Save the database.
    #     ext_out = os.path.splitext(tilename)[1].lower()
    #
    #     if ext_out == ".h5":
    #         tile_df.to_hdf(tilename, "icesat2", complib="zlib", complevel=3, mode='w')
    #     elif ext_out == ".feather":
    #         tile_df.to_feather(tilename,
    #                            compression=self.ivert_config.feather_database_compress_algorithm,
    #                            compression_level=self.ivert_config.feather_database_compress_level)
    #     if verbose:
    #         print(os.path.split(tilename)[1], "written.")
    #
    #     if write_stats:
    #         # Write out the stats to a single-record .csv file.
    #         # For later ingestion into the database.
    #         summary_csv_fname = os.path.splitext(tilename)[0] + "_summary.csv"
    #         # Update the database to reflect that this tile is already written.
    #         # For now, just quickly spit out a csv file.
    #         data_dict = {'filename': [tilename],
    #                      'xmin'    : [bbox_bounds[0]],
    #                      'xmax'    : [bbox_bounds[2]],
    #                      'ymin'    : [bbox_bounds[1]],
    #                      'ymax'    : [bbox_bounds[3]],
    #                      'numphotons'       : [len(tile_df)],
    #                      'numphotons_canopy': [numpy.count_nonzero(tile_df["class_code"].between(2,3,inclusive="both"))],
    #                      'numphotons_ground': [numpy.count_nonzero(tile_df["class_code"] == 1)],
    #                      'numphotons_bathy': [numpy.count_nonzero(tile_df["class_code"] == 4)],
    #                      'is_populated'     : [True]
    #                      }
    #
    #         csv_df = pandas.DataFrame(data=data_dict)
    #         csv_df.to_csv(summary_csv_fname, index=False)
    #         if verbose:
    #             print(os.path.split(summary_csv_fname)[1], "written.")
    #
    #     return tile_df

    def delete_csvs_already_in_database(
        self, gdf=None, force_read_from_disk=False, verbose=True
    ):
        """Sometimes in parallelization, a tile _summary.csv file gets entered into the database
        but never erased from disk. Go through the database,
        delete any CSV files that are already entered as populated in the database.

        Since (with multiprocessing) this version of the ICESat2_photon_database
        geopackage could be more "recent" than what's on disk, use
        'force_read_from_disk' to only use the version of the gpkg that is on disk,
        not the one in memory here."""
        csv_filenames = [
            os.path.join(self.tiles_directory, fname)
            for fname in os.listdir(self.tiles_directory)
            if (re.search(r"_summary\.csv\Z", fname) != None)
        ]
        if force_read_from_disk:
            gdf = geopandas.read_file(self.gpkg_fname, mode="r")
            if verbose:
                print(os.path.split(self.gpkg_fname)[1], "read.")
        elif gdf is None:
            gdf = self.get_gdf(verbose=verbose)
        else:
            assert type(gdf) == geopandas.GeoDataFrame

        num_files_removed = 0
        for csv_fname in csv_filenames:
            tile_fname = csv_fname.replace(
                "_summary.csv", ".h5"
            )  # Right now this only works if .h5 names are in the database. TOOD: Change for future inclusion of .h5 or .feather.
            gdf_record = gdf.loc[gdf.filename == tile_fname]
            assert len(gdf_record) == 1
            if gdf_record["is_populated"].tolist()[0] == True:
                os.remove(csv_fname)
                num_files_removed += 1

        if verbose:
            print(num_files_removed, "tile _summary.csv files removed.")

        return

    def update_gpkg_with_csvfiles(
        self,
        gdf=None,
        use_tempfile=True,
        delete_when_finished=True,
        save_to_disk=True,
        verbose=True,
    ):
        """Look through the photon tiles directory, look for any "_summary.csv" files that have been written.
        Ingest them into the database.

        Sometimes there creates conflicts when this process is writing the gpkg (which takes a WHILE) and
        another process tries to write to it. Help minimize those conflicts with
        'use_tempfile', which will save the geopackage to a temporary-named file first, and then
        rename it to the self.gpkg_fname when it's done, which is quite fast.
        TODO: Implement some kind of an os-level locking convention for this, to fully avoid conflicts in the future.
        But this should be fast enough to keep them to a bare minimum (a dangerous promise, lol).

        If 'delete_when_finished' is set, delete the _summary.csv files after we've
        included them in the database. This is set by default. If the database gets
        corrupted, it can be rebuit with the
            ICESat2_Database.create_new_geopackage(populate_with_existing_tiles = True)
        method+option. This is slower than reading the summary files, but it works.
        """
        if gdf is None:
            gdf = self.get_gdf(verbose=verbose)

        # Get the filenames from the csv files.
        csv_filenames = [
            os.path.join(self.tiles_directory, fname)
            for fname in os.listdir(self.tiles_directory)
            if (re.search(r"_summary\.csv\Z", fname) != None)
        ]
        if verbose and len(csv_filenames) > 0:
            print(
                "Found",
                len(csv_filenames),
                "csv records to update the tile database... ",
                end="",
            )

        # print(gdf)

        for csv_fname in csv_filenames:
            # Read the 1-record CSV from the file.
            csv_gdf = pandas.read_csv(csv_fname)
            # print(csv_gdf)
            # print(csv_gdf['filename'])
            # print(csv_gdf['filename'].tolist())
            # print(csv_gdf['filename'].tolist()[0])
            # insert the record into the database.
            gdf_record = gdf.loc[gdf.filename == csv_gdf["filename"].tolist()[0]]
            # Sometimes the filename can be a .feather but the database still has a .h5 listed in it. Try that instead.
            if len(gdf_record) == 0:
                fname = os.path.splitext(csv_gdf["filename"].tolist()[0])[0] + ".h5"
                gdf_record = gdf.loc[gdf.filename == fname]
            # print(gdf_record)
            idx = gdf_record.index
            # print(idx)
            # All these records should be the same.
            assert len(gdf_record) == 1
            assert gdf_record["xmin"].tolist()[0] == csv_gdf["xmin"].tolist()[0]
            assert gdf_record["xmax"].tolist()[0] == csv_gdf["xmax"].tolist()[0]
            assert gdf_record["ymin"].tolist()[0] == csv_gdf["ymin"].tolist()[0]
            assert gdf_record["ymax"].tolist()[0] == csv_gdf["ymax"].tolist()[0]
            assert csv_gdf["is_populated"].tolist()[0] == True
            # Update the photon counts.
            gdf.loc[idx, "numphotons"] = csv_gdf["numphotons"].tolist()[0]
            gdf.loc[idx, "numphotons_canopy"] = csv_gdf["numphotons_canopy"].tolist()[0]
            gdf.loc[idx, "numphotons_ground"] = csv_gdf["numphotons_ground"].tolist()[0]
            gdf.loc[idx, "is_populated"] = True

        # Update the gdf we have on record, make sure it matches.
        self.gdf = gdf

        if verbose and len(csv_filenames) > 0:
            print("done updating.")

        if len(csv_filenames) > 0 and save_to_disk:
            if verbose:
                print("Writing geopackage...")
            self.save_geopackage(gdf=gdf, use_tempfile=use_tempfile, verbose=verbose)

        if delete_when_finished:
            for csv_fname in csv_filenames:
                os.remove(csv_fname)

        return self.gdf

    def upgrade_database_to_v2(self):
        """The version 2 of the database has:
        - Just the file name to each feather file. Getting rid of full file paths. File paths will be appended using the configfile.
        - adding column 'numphotons_bathy'
        - adding column 'start_date_YYYYMMDD'
        - adding column 'end_date_YYYYMMDD'
        - Save the file back out to disk.

        This method really only needs to be run once.
        After that it can be deprecated and eventually (once the "create_new_database" method has been updated
        to match this table format), it can be eliminated.
        """
        gdf = self.get_gdf()

        print(gdf)
        print(gdf.columns)

        gdf["numphotons_bathy"] = 0
        gdf["start_date_YYYYMMDD"] = 20210101
        gdf["end_date_YYYYMMDD"] = 20211231
        gdf["atl03_version"] = 5

        fnames = gdf["filename"].tolist()
        fnames = [
            os.path.basename(os.path.splitext(fname)[0] + ".feather")
            for fname in fnames
        ]
        gdf["filename"] = fnames

        new_column_order = [
            "filename",
            "xmin",
            "xmax",
            "ymin",
            "ymax",
            "numphotons",
            "numphotons_canopy",
            "numphotons_ground",
            "numphotons_bathy",
            "start_date_YYYYMMDD",
            "end_date_YYYYMMDD",
            "atl03_versionis_populated",
            "geometry",
        ]

        # Do a bit of validation on the files we just hard-coded here.
        assert len(new_column_order) == len(list(gdf.columns.values)) and numpy.all(
            [colname in list(gdf.columns.values) for colname in new_column_order]
        )

        gdf = gdf[new_column_order]

        print(gdf)
        print(gdf.columns)

        self.save_geopackage(gdf=gdf)
        self.save_geopackage(gdf=gdf, compress=True)

    def read_photon_tile(self, tilename):
        """Read a photon tile. If the tilename doesn't exist, return None."""
        # In v1 of the database we saved the entire path of the tile. This may be different on different machines.
        # Instead, use the directory from ivert_config.icesat2_photon_tiles_directory
        # v2 of the database uses just the file names (no directory). This will work either way.

        # If the tilename doesn't start with the directory, add it.
        if self.ivert_config.icesat2_photon_tiles_directory not in tilename:
            tilename = os.path.join(
                self.ivert_config.icesat2_photon_tiles_directory,
                os.path.basename(tilename),
            )

        # Look for either a .h5 or .feather tile. If neither exists, return None.
        base, ext = os.path.splitext(tilename)
        assert ext.lower() in (".h5", ".feather")

        feather_name = base + ".feather"
        h5_name = base + ".h5"

        # If the file doesn't exist locally but we're in the AWS cloud, see if we can download it from an S3 bucket.
        if (
            not os.path.exists(feather_name)
            and not os.path.exists(h5_name)
            and self.ivert_config.is_aws
        ):
            s3_manager = self.get_s3_manager()
            s3_photon_tiles_dir = self.ivert_config.s3_photon_tiles_directory_prefix

            # If the local directory doesn't exist, create it.
            # This may happen when on a new S3 instance.
            if not os.path.exists(self.ivert_config.icesat2_photon_tiles_directory):
                os.makedirs(self.ivert_config.icesat2_photon_tiles_directory)

            assert s3_photon_tiles_dir is not None
            s3_feather_key = (
                s3_photon_tiles_dir.rstrip("/") + "/" + os.path.basename(feather_name)
            )
            s3_h5_key = (
                s3_photon_tiles_dir.rstrip("/") + "/" + os.path.basename(h5_name)
            )
            if s3_manager.exists(s3_feather_key):
                s3_manager.download(s3_feather_key, feather_name)
            elif s3_manager.exists(s3_h5_key):
                s3_manager.download(s3_h5_key, h5_name)
            else:
                return None

        # To make the HDF5 and Feather formats basically interchangeable, first look for the one.
        # Then if you can't find it, look for the other.
        # Try the feather file first.
        try:
            if os.path.exists(feather_name):
                return pandas.read_feather(feather_name)
            elif os.path.exists(h5_name):
                return pandas.read_hdf(h5_name, mode="r")
        except (
            pyarrow.lib.ArrowInvalid,
            pyarrow.lib.ArrowIOError,
            pyarrow.lib.ArrowKeyError,
            tables.exceptions.HDF5ExtError,
            tables.exceptions.NodeError,
            tables.exceptions.FlavorError,
        ):
            pass

        # If neither of those work, return None if the file is not found.
        return None

    def get_tiling_progress_mapname(self):
        """Output a map of the tiling progress so far.
        This must be called within download_all_icesat2_granules.py to avoid circular import conflicts.
        """
        return os.path.abspath(
            os.path.splitext(self.gpkg_fname)[0] + "_progress_map.png"
        )

    def update_and_fix_photon_database(self, start_i=0, end_i=None, verbose=True):
        """Sometimes the download_all_icesat2_granules.py -- photon_tiling process
        creates files without updating the database correctly.

        This will loop through all entries in the database, as well as all files, and
        check:
            1) That all entries with "is_populated" actually have valid files associated with them.
            2) That each of those files has the correct number of photons in it, matching up with "numphotons"
            3) That all database files with valid data are included in the database.
            4) If featherize, change .h5 files to .feather files.

        It will fix any errors it finds. If files are corrupted, it will delete them and zero-out the
        entry in the database so they can be rebuilt.
        """
        gdf = self.get_gdf(verbose=verbose)
        # print(gdf.columns)
        # print(numpy.count_nonzero(gdf.is_populated), "populated.")
        # print(numpy.count_nonzero(~gdf.is_populated), "not populated.")

        num_corrected = 0
        num_zero = 0

        bar_str = ""
        for idx, row in itertools.islice(
            gdf.iterrows(),
            start_i,
            end_i if ((end_i is not None) and (end_i < len(gdf))) else None,
        ):
            i = int(idx)
            # if i < start_i:
            #     continue
            # elif (end_i is not None) and (i > end_i):
            #     break
            # # print(row.filename)
            # if idx==10:
            #     break
            fname = row.filename
            feather_name = os.path.splitext(fname)[0] + ".feather"
            # h5_name = os.path.splitext(fname)[0] + ".h5"

            assert os.path.exists(feather_name)
            df = pandas.read_feather(feather_name)
            # print(df.columns)
            if len(df) != row.numphotons:
                print("\r" + (" " * len(bar_str)) + "\r", end="")
                print("Row", idx, "needs fixing.", len(df), row.numphotons)
                gdf.loc[idx, "numphotons"] = len(df)
                gdf.loc[idx, "numphotons_canopy"] = numpy.count_nonzero(
                    df["class_code"].between(2, 3, inclusize="both")
                )
                gdf.loc[idx, "numphotons_ground"] = numpy.count_nonzero(
                    df["class_code"] == 1
                )
                assert gdf.loc[idx, "numphotons"] == (
                    gdf.loc[idx, "numphotons_canopy"]
                    + gdf.loc[idx, "numphotons_ground"]
                )

                num_corrected += 1

            if len(df) == 0:
                num_zero += 1

            # print("\r" + (" "*len(bar_str)) + "\r", end="")
            # print(os.path.basename(feather_name), len(df), "photons.")

            # Print out a status line every 500th tile.
            if verbose and ((i == 0) or ((i % 500) == 499)):
                print("\r" + (" " * len(bar_str)) + "\r", end="")
                print(
                    "{0}/{1}".format(i + 1, len(gdf)),
                    os.path.basename(feather_name),
                    len(df),
                    "photons.",
                )

            if verbose:
                bar_str = utils.progress_bar.ProgressBar(
                    i + 1, len(gdf), suffix="{0}/{1}".format(i + 1, len(gdf))
                )

            # if idx==250:
            #     break

        print("\r" + (" " * len(bar_str)) + "\r", end="")
        print(num_corrected, "mismatched entries.", num_zero, "zero-photon entries.")

        if num_corrected > 0:
            self.save_geopackage(gdf=gdf, verbose=verbose)
        return

    def delete_zero_entry_tiles(self):
        """Find tiles that have zero photons in them. Delete those tiles, and their entries from the database.
        NOTE: This should only be done after "update_and_fix_photon_database()" has been run."""
        # TODO: Finish.

    ### The next 4 methods all have to do with "subsetting" the ICESat-2 granule photon databases into smaller chunks
    # to help optimize the generation of ICESat-2 photon_tiles. Near the pole-hole in Antartica, the subsetting of
    # all these granule tiles takes FOREVER, and it was really becoming a beast. Here we split them up, and use the
    # give_granule_subset_name() to give a subset name for a given location, making it easy for code above to look for a
    # subset file rather than the full granule file. The "subset_remaining_granules" method immediately below has default
    # parameters to subset all the granules that lie within the bounds of Antarctica that we haven't yet completed tiles for.
    # Previous definition, for doing east-side
    # def subset_remaining_granules(self, lon_min=-136, lon_max=180, lat_min=-90, lat_max=-86, lon_deg_chunksize=2, icesat2_region_num=11):
    # Below: Last for for east-side bottom-row only.
    # TODO: After runnign this form -138 to -136 to cover the gap left by the error of not capping the bin boundaries,
    #    then re-run it with lon_min at -180 and mask out all if i<3180. Change the "proc 4" if-statement to do that.
    def subset_remaining_granules(
        self,
        lon_min=-180,
        lon_max=-136,
        lat_min=-90,
        lat_max=-88,
        lon_deg_chunksize=2,
        icesat2_region_num=11,
    ):
        """Take the remaining granules that are in the icesat2_granules_subset_directory that match a certain region number, and subset them into chucks by longitude.
        This will make subsetting them much easier down the line."""
        bin_boundaries = numpy.arange(
            lon_min, lon_max + (lon_deg_chunksize * 0.5), lon_deg_chunksize
        )

        granule_dir = self.ivert_config._abspath(
            self.ivert_config.icesat2_granules_directory
        )
        granule_fnames = os.listdir(granule_dir)
        if icesat2_region_num is not None:
            granule_fnames = [
                fn
                for fn in granule_fnames
                if int(
                    re.search(
                        r"(?<=ATL03_(\d){14}_(\d){6})(\d){2}(?=_005_01_photons)", fn
                    ).group()
                )
                == icesat2_region_num
            ]

        granule_fnames = [os.path.join(granule_dir, fn) for fn in granule_fnames]

        # external_drive = self.ivert_config._abspath(self.ivert_config.icesat2_granules_directory_alternate)

        for i, gfn in enumerate(granule_fnames):
            n_written = self.subset_individual_granule(
                gfn,
                bin_boundaries,
                lat_min=lat_min,
                lat_max=lat_max,
                lon_deg_chunksize=lon_deg_chunksize,
            )
            print(
                "{0}/{1}".format(i + 1, len(granule_fnames)),
                os.path.basename(gfn),
                "->",
                n_written,
                "subset files.",
            )

    def subset_individual_granule(
        self,
        granule_name,
        bin_boundaries,
        lat_min=-90,
        lat_max=-86,
        lon_deg_chunksize=2,
    ):
        """Take an individual icesat-2 granule, divide it up into all its counterparts, save them to subset files."""
        outfiles = self.list_of_granule_subset_names(
            granule_name, bin_boundaries, lon_deg_chunksize=lon_deg_chunksize
        )
        # If all the subset files already exist, just exit.
        if numpy.all([os.path.exists(of) for of in outfiles]):
            return 0
        assert os.path.splitext(granule_name)[1] == ".feather"
        df = pandas.read_feather(granule_name)
        lons = df.longitude
        max_lon = lons.max()
        min_lon = lons.min()

        # Quick check, in the instance where we're only looking at one bin, if the photons all fall outside the bounds
        # of that bin, then just move on and don't consider this one.
        if len(bin_boundaries) == 2 and (
            (bin_boundaries[0] > max_lon) or (bin_boundaries[1] < min_lon)
        ):
            return 0
        # Subtract 1 to put the first bin at index 0, everything to the left as index -1. This will ensure the for statement
        # below misses the first bin (everything left of the minimum longitude).
        photon_bin_nums = numpy.digitize(lons, bin_boundaries) - 1
        # print(numpy.max(photon_bin_nums), len(bins_left), len(outfiles))
        # print("photon_bin_nums", photon_bin_nums)
        # print("bins_left", bins_left)
        # print("outfiles", [os.path.basename(of) for of in outfiles])
        max_bin_num = numpy.max(photon_bin_nums)
        assert max_bin_num <= len(bin_boundaries) == (len(outfiles) + 1)
        num_written = 0

        # print(os.path.basename(granule_name), len(photon_bin_nums), "photons.")
        # Omit the last "photon_bin_num" bin, we're just using that as a cutoff beyond the "max."
        for bin_id, left_lon, right_lon, of in zip(
            numpy.arange(0, max_bin_num),
            bin_boundaries[:-1],
            bin_boundaries[1:],
            outfiles,
        ):
            # If it already exists, just skip it.
            if os.path.exists(of):
                continue
            # If absolutely none of the photons falls within this bin boundary, just skip.
            if (left_lon > max_lon) or (right_lon < min_lon):
                continue

            # First just subset by the bin_ids (longitude).
            subset_mask = photon_bin_nums == bin_id
            # If there's no photons in this longitude segment, skip it.
            if not numpy.any(subset_mask):
                continue

            # Cut off all points between lat-min and lat-max.
            if lat_min is not None:
                lats = df.latitude
                if lat_max is None:
                    subset_mask = subset_mask & (lats >= lat_min)
                else:
                    subset_mask = subset_mask & lats.between(
                        lat_min, lat_max, inclusive="left"
                    )
            elif lat_max is not None:
                lats = df.latitude
                subset_mask = subset_mask & (lats < lat_max)
            # If there's no photons in this longitude segment after filtering out latitudes, skip it.
            if not numpy.any(subset_mask):
                continue

            # Subset the photons according to the bounding  box.
            df_subset = df[subset_mask]
            df_subset.reset_index().to_feather(of)
            num_written += 1
            # print("  " + os.path.basename(of), "written.", len(df_subset), "photons.")

        return num_written

    def list_of_granule_subset_names(
        self, granule_name, bin_boundaries, lon_deg_chunksize=2, fmt=".feather"
    ):
        """For a given ICESat-2 granule name, return all the granule subset names associated with it."""
        return [
            self.give_granule_subset_name(
                granule_name, lon, lon_deg_chunksize=lon_deg_chunksize, fmt=fmt
            )
            for lon in bin_boundaries[:-1]
        ]

    def give_granule_subset_name(
        self, granule_name, longitude, lon_deg_chunksize=2, fmt=".feather"
    ):
        """Given an ICESat-2 granule name, return the names of the granule_subset file that should correspond to it."""
        fname = os.path.basename(granule_name)
        base, ext = os.path.splitext(fname)
        dirname_out = self.ivert_config._abspath(
            self.ivert_config.icesat2_granules_subset_directory
        )

        bin_left = int(numpy.floor(longitude / lon_deg_chunksize) * lon_deg_chunksize)
        bin_right = int(bin_left + lon_deg_chunksize)

        # Append a lon_min and lon_max to the filename.
        basename_out = base + "_{1}{0:03d}_{3}{2:03d}".format(
            abs(bin_left),
            "W" if (bin_left < 0) else "E",
            abs(bin_right),
            "W" if (bin_right < 0) else "E",
        )
        return os.path.join(dirname_out, basename_out + fmt)

    def check_integrity(
        self, logfile: str = None, fix_db_mistakes: bool = True, verbose: bool = True
    ) -> str:
        """Check the integrity of all the files in the photon database, and that they match the database records.

        Args:
            logfile (str): The name of the log file to write to. If None, no log file will be written.
            fix_db_mistakes (bool): If True, fix database mismatches with the photon files.
            verbose (bool): If True, print extra information.

        Returns:
            str: The text contents of the log file.
        """
        tilenames = self.get_gdf(verbose=verbose).filename.to_list()
        tiles_good = []
        tiles_missing = []
        tiles_w_mistakes = []
        gdf = self.get_gdf()

        for i, tname in enumerate(tilenames):
            df = self.read_photon_tile(tname)

            if df is None:
                tiles_missing.append(tname)
                if verbose:
                    utils.progress_bar.ProgressBar(
                        i + 1, len(tilenames), suffix=f"{i + 1}/{len(tilenames)}"
                    )
                continue

            # Fetch the record for this tile.
            row = gdf[gdf.filename == tname].iloc[0]
            if row["numphotons"] == len(df):
                tiles_good.append(tname)
            else:
                tiles_w_mistakes.append(tname)
                if fix_db_mistakes:
                    gdf_subset = gdf[gdf.filename == tname]
                    gdf_subset["numphotons"] = len(df)
                    gdf_subset["numphotons_canopy"] = numpy.count_nonzero(
                        df["class_code"].between(2, 3, inclusive="both")
                    )
                    gdf_subset["numphotons_ground"] = numpy.count_nonzero(
                        df["class_code"] == 1
                    )
                    gdf_subset["numphotons_bathy"] = numpy.count_nonzero(
                        df["class_code"] == 4
                    )

            if self.ivert_config.is_aws:
                # Delete the local tile after we're done with it, if in on the AWS environment.
                # ONLY do this on the AWS environment, where tiles are stored in the s3 bucket but used locally.
                local_tile = os.path.join(
                    self.ivert_config.icesat2_photon_tiles_directory, tname
                )
                if os.path.exists(local_tile):
                    os.remove(local_tile)

            if verbose:
                utils.progress_bar.ProgressBar(
                    i + 1, len(tilenames), suffix=f"{i + 1}/{len(tilenames)}"
                )

        if fix_db_mistakes and (len(tiles_w_mistakes) > 0):
            if verbose:
                print(
                    f"Fixing {len(tiles_w_mistakes)} tiles with 'numphotons' mismatches."
                )
                print(f"Writing {self.gpkg_fname}.")
            self.save_geopackage(gdf=gdf, use_tempfile=True, compress=False)

            if verbose:
                print(f"Writing {self.gpkg_fname_compressed}.")
            self.save_geopackage(gdf=gdf, use_tempfile=True, compress=True)

        log_text = f"{len(tiles_good)} tiles are good.\n"
        log_text += f"{len(tiles_missing)} tiles are missing.\n"
        log_text += f"{len(tiles_w_mistakes)} tiles have 'numphotons' mismatches"
        if fix_db_mistakes:
            log_text += " and were fixed.\n"
        else:
            log_text += ".\n"

        if verbose:
            print(log_text)

        if logfile is not None:
            with open(logfile, "w") as f:
                f.write(log_text)

        return log_text


if __name__ == "__main__":
    is2db = ICESat2_Database()
    if not os.path.exists(is2db.gpkg_fname_compressed):
        gdf = is2db.get_gdf()
        utils.pickle_blosc.write(gdf, is2db.gpkg_fname_compressed)
        print(is2db.gpkg_fname_compressed, "written.")
