
import numpy
import rasterio
import shapely.geometry
import os
import geopandas
import sys

format_dict = {"gpkg": ".gpkg",
               "geopackage": ".gpkg",
               "shp": ".shp",
               "shapefile": ".shp",
               "xyz": ".xyz"}

def convert_ivert_error_map_to_vector(ivert_error_tifs, output_format="gpkg", overwrite=True, verbose=True):
    """Convert an IVERT raster error_map.tif file into a vector file for easier viewing in GIS programs."""

    # Open the raster
    for tif_file in ivert_error_tifs:
        src = rasterio.open(tif_file, mode='r')
        array = src.read(1)

        ndv = src.nodatavals[0]

        good_rows, good_cols = numpy.where((~array.isnan()) if numpy.isnan(ndv) else (array != ndv))
        good_xs, good_ys = rasterio.transform.xy(src.transform, good_rows, good_cols, offset='center')
        good_data = array[good_rows, good_cols]
        # print(min(good_data), max(good_data))
        crs = src.crs

        data_dict = {'error_val': good_data,
                     'geometry': [shapely.geometry.Point(x, y) for (x,y) in zip(good_xs, good_ys)]}

        gdf = geopandas.GeoDataFrame(data=data_dict, geometry='geometry', crs=crs)

        # Convert the filename to the output vector format.
        output_ext = format_dict[output_format.strip().lower()]
        vector_fname = os.path.splitext(tif_file)[0] + (output_ext if (output_ext[0] == ".") else ('.' + output_ext))

        if os.path.exists(vector_fname):
            if overwrite:
                os.remove(vector_fname)
            else:
                if verbose:
                    print(vector_fname, "already exists.")
                return

        gdf.to_file(vector_fname)
        if verbose:
            print(vector_fname, "written with", len(gdf), "points.")

    return


if __name__ == "__main__":
    filenames = sys.argv[1:]
    convert_ivert_error_map_to_vector(filenames)