# Standar python libraries
import pandas
import geopandas
import os
import shapely.geometry
import osgeo.gdal as gdal
import pyproj
import argparse
import subprocess

# My scripts
import utils.configfile
import utils.pyproj_funcs as pyproj_funcs
import utils.parallel_funcs


class ValidationChild:
    def __init__(
        self,
        tile_gpkg_fname,
        # Presumably, a subset GPKG of the total tile database, given by the validation_pool_manager.
        connection,
        dem_fname,
        dem_mask_file=None,
        tempdir=None,
        measure_coverage=False,
        coverage_subdivisions=15,
    ):
        """Initiate a child process instance.

        This also initiates the validation loop that will be looking for processing data from the validation_pool_parent
        process. The loop will be idle until work is given to it.
        """
        self.tile_dbname = tile_gpkg_fname
        self.dem_fname = dem_fname
        self.dem_mask_fname = dem_mask_file
        self.connection = connection
        self.tile_gpkg_fname = (tile_gpkg_fname,)
        self.tempdir = tempdir
        self.measure_coverage = measure_coverage
        self.coverage_subdivisions = coverage_subdivisions
        # Dict of tile dataframe IDXs to dataframes.
        # (int: None) or (int: dataframe) for key,value pairs.
        self.photon_df_dict = {}

        (
            self.tile_gdf,
            self.dem_epsg,
            self.dem_gt,
            self.dem_xsize,
            self.dem_ysize,
        ) = self.pre_process_data()

        # Now, loop until we've validated everything.
        self.validation_loop()

    def validation_loop(self):
        """The main loop to keep processing validation input from the parent, using the connection."""
        tile_gdf = self.get_tile_gdf()
        tile_ids = tile_gdf.index.to_list()

        # Get info from the parent process via the connection, and start processing. Keep looking until receiving a
        # "STOP" command.
        # TODO: Finish

    def pre_process_data(self):
        """Things to do at initialization.

        1) Read the DEM epsg and geotransform from the DEM file headers.

        2) Verify that the tile CRM epsg (passed from the parent process) matches the epsg of the DEM.
        """
        # Get the espg of the horizontal projection of the geodataframe
        tile_gdf = self.get_tile_gdf()
        tile_gdf_epsg = pyproj_funcs.get_horizontal_projection_only(
            tile_gdf.crs, as_epsg=True
        )

        # Get the epsg of the horizonal projection of the DEM, and other DEM info from the file header.
        ds = gdal.Open(self.dem_fname, gdal.GA_ReadOnly)
        dem_epsg = pyproj_funcs.get_horizontal_projection_only(
            ds.GetProjection(), as_epsg=True
        )
        dem_gt = ds.GetGeoTransform()
        dem_xsize = ds.RasterXSize
        dem_ysize = ds.RasterYSize
        # Close the file by de-referencing the variable.
        del ds

        # Make sure the tile database matches the same EPSG as the DEM
        assert tile_gdf_epsg == dem_epsg

        return tile_gdf, dem_epsg, dem_gt, dem_xsize, dem_ysize

    def get_tile_gdf(self):
        """Return the tile geodataframe. Read from disk if necessary."""
        # Only read it from disk if we don't already have it in memory.
        if self.tile_gdf is None:
            ext = os.path.splitext(self.tile_dbname)[1].lower()
            if ext == ".gpkg":
                self.tile_gdf = geopandas.read_file(self.tile_dbname)
            elif ext in (".gz", ".pickle"):
                self.tile_gdf = pandas.read_pickle(self.tile_dbname)
            else:
                raise NotImplementedError(
                    "Unhandled extension '{0}' in database file {1}.".format(
                        ext, os.path.basename(self.tile_dbname)
                    )
                )

        # The tile database should be projected into the same projection as the DEM. This should already be handled
        # by the parent process, don't do it here. Just assert that it's true. Can remove this check later once the
        # code has been adequately stress-tested (or leave it in here as a safety-barrier, as shit might not work at all
        # and give unexpected erroneous results if we're comparing two different projetions).
        assert self.dem_epsg == self.tile_gdf.crs.to_epsg()

        return self.tile_gdf

    def compute_ij_coordinates_of_photons(self, tile_df, remove_if_out_of_bounds=True):
        """Given a photon tile, add 'i' and 'j' coordinates to the dataframe.

        (i, j) corresponds to the (row, col) / (y, x) indices of the raster images being validated.
        Any photons that fall outside of the image
        """

    def get_bbox_from_ij(self, i: int, j: int) -> tuple:
        """Given an i,j (row, col) raster coordinate, get a bounding box (xmin, ymin, xmax, ymax) of that grid cell."""
        xstart, xstep, _, ystart, _, ystep = self.dem_gt
        assert (xstep > 0) and (ystep < 0)

        xmin = xstart + (j * xstep)
        ymin = ystart + (i * (ystep + 1))
        xmax = xmin + xstep
        ymax = ymin - ystep

        return xmin, ymin, xmax, ymax

    def find_tiles_overlapping_bbox(self, bbox):
        """Given a (minx, miny, maxx, maxy) bounding box, find tiles that overlap that box.

        Return the subset of the dataframe that overlaps, including the index and filename
        """
        polygon = shapely.geometry.box(*bbox)

        gdf = self.get_tile_gdf()

        # First, subset the tiles using the spatial index bounding box.
        gdf_sub1 = gdf.loc[gdf.sindex.query(polygon)]
        # Then find all tiles in that subset that overlap but do not just "touch" on an edge or corner.
        gdf_subset = gdf_sub1[gdf_sub1.intersects(polygon) & ~gdf_sub1.touches(polygon)]

        return gdf_subset

    # def validate_dem_child_process(self, input_heights, input_i, input_j, photon_codes, connection, photon_limit=None,
    #                                measure_coverage=False, input_x=None, input_y=None, num_subdivisions=15):
    #     pass


class ValidationManager:
    # Class variable for the path of where temp directories should go.
    tempdir_prefix = "validation_temp_"

    def __init__(
        self,
        dem_fname,
        dem_mask_file=None,
        working_tempdir=None,
    ):
        """If a working_tempdir is provided, the directory should already exist when this object is created."""
        # Name of the DEM to be validated.
        self.dem_fname = dem_fname
        # Name of the boolean mask file indicating which cells in the DEM should be validated. If one is not provided, we will create it.
        # The mask file should have an identical extent and horizontal projection as the DEM.
        self.dem_mask_file = dem_mask_file
        # The EPSG value of the horizontal projection of the DEM.
        self.dem_epsg = None
        # The geographic or projected bounding box of the DEM as a 4-tuple. (xmin, ymin, xmax, ymax)
        self.dem_bbox = None
        # The configfile for this prociess.
        self.config = utils.configfile.Config()
        # The temporary directory for this process.
        if working_tempdir is None:
            self.proc_tempdir = self._create_new_tempdir(delete_existing=True)
        else:
            assert os.path.exists(working_tempdir)
            self.proc_tempdir = working_tempdir
        # The subset of the photon_tiles GDF that overlaps this DEM.
        self.tile_subset_gdf_fname = None
        self.tile_subset_gdf = None

    def _create_new_tempdir(self, delete_existing: bool = True) -> str:
        procnum = os.getpid()
        pathname = os.path.join(
            self.config.cudem_cache_directory,
            ValidationManager.tempdir_prefix + str(procnum),
        )
        # If the tempdir currently exists, delete it. This should be safe because no other process should be using it.
        if os.path.exists(pathname) and delete_existing:
            rm_cmd = ["rm", "-rf", pathname]
            subprocess.run(rm_cmd, capture_output=True)
            os.mkdir(pathname)

        return pathname

    def get_dem_metadata(self) -> tuple:
        if self.dem_epsg is None or self.dem_bbox is None:
            dem_ds = gdal.Open(self.dem_fname, gdal.GA_ReadOnly)
            self.dem_epsg = pyproj_funcs.get_horizontal_projection_only(
                dem_ds.GetProjection(), as_epsg=True
            )
            dem_xsize = dem_ds.RasterXSize
            dem_ysize = dem_ds.RasterYSize
            xstart, xstep, _, ystart, _, ystep = dem_ds.GetGeoTransform()
            assert (xstep > 0) and (ystep < 0)
            self.dem_bbox = (
                xstart,
                ystart + (dem_ysize * ystep),
                xstart + (dem_xsize * xstep),
                ystart,
            )

        return self.dem_epsg, self.dem_bbox

    def _subset_and_reproject_tile_database(self, verbose=True):
        """Before starting the validations, subset and reproject the tile database into the DEM crs.

        Create the tempdir for processing, and put this database into it. This is what the sub-processes will use."""
        is2db = icesat2_photon_database.ICESat2_Database()
        gdf = is2db.open_gdf()

        # Get the DEM metadata if we haven't yet.
        if self.dem_epsg is None or self.dem_bbox is None:
            self.dem_epsg, self.dem_bbox = self.get_dem_metadata()

        # Include only polygons within the defined WGS84 coverage area of the projection.
        bounding_polygon = shapely.geometry.box(
            *pyproj.CRS.from_user_input(f"EPSG:{self.dem_epsg}").area_of_use.bounds
        )
        # If the WGS84 bounds of this projection aren't global, then subset the gdf before projecting it.
        if bounding_polygon.bounds != (-180.0, -90.0, 180.0, 90.0):
            gdf_sub1 = gdf.loc[gdf.sindex.query(bounding_polygon)]
            # Then find all tiles in that subset that overlap but do not just "touch" on an edge or corner.
            gdf = gdf_sub1[
                gdf_sub1.intersects(bounding_polygon)
                & ~gdf_sub1.touches(bounding_polygon)
            ]

        # Now, transform the GDF into the new projection. Then, we'll subset it by only the tile polygons that overlap
        # the DEM bounding box.
        # We only need to transform the GDF if the DEM is not in WGS84.
        # NOTE: This may break with polar projetions (where a DEM may include the pole). Need to test this out and
        # modify accordingly if it does.
        if self.dem_epsg != pyproj_funcs.get_horizontal_projection_only(
            gdf.crs, as_epsg=True
        ):
            gdf = gdf.to_crs(f"EPSG:{self.dem_epsg}")

        # Now, subset the GDF to only the tiles overlapping the extent of the DEM.
        dem_polygon = shapely.geometry.box(*self.dem_bbox)
        gdf_sub1 = gdf.loc[gdf.sindex.query(dem_polygon)]
        # Then find all tiles in that subset that overlap but do not just "touch" on an edge or corner.
        # This is the list of icesat-2 photon tiles that overlap the DEM, that we'll use to validate.
        gdf_sub2 = gdf_sub1[
            gdf_sub1.intersects(dem_polygon) & ~gdf_sub1.touches(dem_polygon)
        ]

        # Now, save to the tempdir for sub-processes to use.
        outfile_name = os.path.join(
            self.proc_tempdir,
            os.path.splitext(os.path.basename(self.config.icesat2_photon_geopackage))[0]
            + "_"
            + str(os.getpid())
            + ".pickle",
        )
        self.tile_subset_gdf_fname = outfile_name
        # Make of a copy of the subset database to eliminate any references back to the larger database (free up memory)
        self.tile_subset_gdf = gdf_sub2.reset_index(drop=True)

        self.tile_subset_gdf.to_pickle(outfile_name, compression=None)
        if verbose:
            fname_to_print = outfile_name.replace(
                self.config.project_base_directory, ""
            )
            print(
                os.path.basename(self.dem_fname),
                "overlaps",
                len(gdf_sub2),
                "photon tiles. Written to",
                fname_to_print,
            )

        # Eliminate references to these variables to clean up memory.
        del gdf, gdf_sub1, gdf_sub2

        return self.tile_subset_gdf

    def validate_dem(
        self,
        subproc_chunksize=20,
        photon_limit_per_cell=600,
        measure_coverage=True,
        num_subprocs=utils.parallel_funcs.physical_cpu_count(),
    ):
        """Validate the DEM in parallel, using subprocesses to handle the validation data."""
        # TODO: Flesh out database alignment, sub-process management.

    def __del__(self):
        """The deletion function. Delete the tempdir and all its files."""
        # If the tempdir was created by this object and the directory still exists when the object is being destroyed,
        # delete the tempdir. This is the only process with this pid. This assumes that no other process is currently
        # actively using that directory.
        if (
            self.proc_tempdir is not None
            and os.path.exists(self.proc_tempdir)
            and os.path.dirname(self.proc_tempdir) == self.config.cudem_cache_directory
            and os.path.basename(self.proc_tempdir).find(
                ValidationManager.tempdir_prefix
            )
            == 0
        ):
            rm_cmd = ["rm", "-rf", self.proc_tempdir]
            subprocess.run(rm_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clear_validation_tempdirs(verbose=True):
    """Clear out all the 'validation_temp_' directories in the scratch_data folder.

    This should only be done if other processes aren't currently using the scratch directory for temporary"""
    config = utils.configfile.Config()
    parent_dir = config.cudem_cache_directory
    dir_prefix = ValidationManager.tempdir_prefix
    tempdirs_list = [
        os.path.join(parent_dir, dname)
        for dname in os.listdir(parent_dir)
        if (dname.find(dir_prefix) == 0)
    ]

    rm_cmd = ["rm", "-rf"] + tempdirs_list
    if verbose:
        print(" ".join(rm_cmd))
    subprocess.run(tempdirs_list, capture_output=not verbose)
    if verbose:
        print(len(tempdirs_list), "temporary directories deleted.")

    return


def define_and_parse_args():
    """Define and parse command-line arguments given to this module."""
    parser = argparse.ArgumentParser(
        "Utilities for managing and running validation process pools. "
        + "And some related side utilities. At this time, this module does nothing if "
        + "run without using any of the optional arguments (see below)."
    )
    parser.add_argument(
        "--clear_tempdirs",
        action="store_true",
        default=False,
        help="Remove all validation process tempdirs from the scratch directory. This is helpful if previous processes were killed or terminated and orphaned temp directories exist.",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", default=False, help="Run quietly."
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = define_and_parse_args()

    if args.clear_tempdirs:
        clear_validation_tempdirs(verbose=not args.quiet)
