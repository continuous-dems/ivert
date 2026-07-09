"""Helper functions dealing with geographic projections."""

import pyproj
from osgeo import gdal


def get_bounding_box_and_step(gdal_dataset, bbox_interleaved=True):
    """Get the [xmin, ymin, xmax, ymax] from the gdal geotransform, as well as the [xstep, ystep].

    If 'interleaved', bbox returned as [xmin, ymin, xmax, ymax]
                                 else, [xmin, xmax, ymin, ymax].

    Most processes want interleaved coordinates, but waffles prefers them non-interleaved.
    """
    geotransform = gdal_dataset.GetGeoTransform()
    x_size, y_size = gdal_dataset.RasterXSize, gdal_dataset.RasterYSize

    xmin, xstep, _, ymin, _, ystep = geotransform

    # print("geotransform", geotransform)
    # print('x_size', x_size, "y_size", y_size)

    xmax = xmin + (xstep * x_size)
    ymax = ymin + (ystep * y_size)

    # The geotransform can be based on any corner with negative step-sizes.
    # Get the actual min/max by taking the min() & max() of each pair.
    if bbox_interleaved:
        # Everything else is looking for (xmin,ymin,xmax,ymax)
        return [min(xmin, xmax),
                min(ymin, ymax),
                max(xmin, xmax),
                max(ymin, ymax)], \
            [abs(xstep), abs(ystep)]
    else:
        # The waffles command wants (xmin,xmax,ymin,ymax)
        return [min(xmin, xmax),
                max(xmin, xmax),
                min(ymin, ymax),
                max(ymin, ymax)], \
            [abs(xstep), abs(ystep)]


def get_horizontal_projection_only(gdal_ds_crs_wkt_or_epsg, as_epsg: bool = True):
    """Given an input projection, which may be a combined horizontal+vertical projection, return only the horizontal
    projection.

    gdal_ds_prj_wkt_or_epsg can be any of the following:
    - An open gdal.Dataset object.
    - A pyproj.crs.CRS object
    - A string of well-known-text projection
    - An integer EPSG code (usually 4- or 5-digit)

    If the input projection is horizontal only, it will return the projection identical to the input.
    If the input projection is a combined horizontal+vetical, it will return the horizontal projection only.

    If as_epsg is true, it will return an integer EPSG value.
    If as_egsp is False, it will return a pyproj.crs.CRS object.

    If the input is an unhandled datatype (includine NoneType), or unreadable, None will be returned."""
    # If it's a gdal Dataset object, get the projection and turn it into a pyproj object.
    if isinstance(gdal_ds_crs_wkt_or_epsg, gdal.Dataset):
        prj = pyproj.crs.CRS.from_wkt(gdal_ds_crs_wkt_or_epsg.GetProjection())

    # If it's an integer, presume it's an EPSG.
    elif type(gdal_ds_crs_wkt_or_epsg) == int:
        prj = pyproj.crs.CRS.from_epsg(gdal_ds_crs_wkt_or_epsg)

    # If it's a string, presume it's a WKT, proj-string, or other user input (let the from_user_input() method handle that).
    elif type(gdal_ds_crs_wkt_or_epsg) == str:
        prj = pyproj.crs.CRS.from_user_input(gdal_ds_crs_wkt_or_epsg)

    # If it's already a pyproj.crs.CRS object, just use it.
    elif isinstance(gdal_ds_crs_wkt_or_epsg, pyproj.crs.CRS):
        prj = gdal_ds_crs_wkt_or_epsg

    # If it's none of these, return None (this includes if NoneType is given)
    else:
        return None

    # If it has two or more sub_crs objects with it, then it's a combined projection and the first one in the list is the horizontal projection.
    # Extract it.
    if len(prj.sub_crs_list) >= 2:
        prj = prj.sub_crs_list[0]

    # Return either as an EPSG number or a pyprjo.crs.CRS object.
    if as_epsg:
        epsg = prj.to_epsg()
        # Some of the CUDEM tiles are in NAD83 but the CRS doesn't explicitly give an
        # EPSG code. Handle that special case manually here.
        if epsg is None and prj.to_wkt().find('GEOGCS["NAD83",') >= 0:
            epsg = 4269
        return epsg
    else:
        return prj


def get_dataset_epsg(gdal_dataset, warn_if_not_present=True, horizontal_only=False):
    """Get the projection EPSG value from the dataset, if it's defined."""

    # Testing some things out.
    wkt = gdal_dataset.GetProjection()
    prj = pyproj.crs.CRS.from_wkt(wkt)

    # Some projections are combined (horizontal + vertical). If we only want the horizontal, retrieve that useing the sub-crs values.
    # When it's a horizontal + vertical CRS, the horizontal comes first.
    if horizontal_only:
        prj = get_horizontal_projection_only(prj, as_epsg=False)

    epsg = prj.to_epsg()
    # Some of the CUDEM tiles are in NAD83 but the CRS doesn't explicitly give an
    # EPSG code. Handle that manually here.
    if epsg is None:
        # print(wkt)
        if wkt.find('GEOGCS["NAD83",') >= 0:
            return 4269
        elif warn_if_not_present:
            raise UserWarning("File {0} has no retrievable EPSG value.".format(gdal_dataset.GetFileList()[0]))
        return epsg

    else:
        return epsg


# (EPSG: bbox) pairs for polar stereo projections.
# Gotten from https://spatialreference.org/ref/?search=Polar+Stereographic
# Coordinate bounding-boxes (xmin, ymin, xmax, ymax) in WGS84 lat/lon
# polar_stereo_bounds_dict = \
#     {2985: (139.5000, -66.9000, 140.5000, -66.5000),
#      2986: (135.0000, -68.0000, 142.0000, -66.0000),
#      3031: (-180.0000, -90.0000, 180.0000, -60.0000),
#      3032: (45.0000, -90.0000, 160.0000, -60.0000),
#      3275: (-180.0000, -84.0000, -150.0000, -80.0000),
#      3276: (-150.0000, -84.0000, -120.0000, -80.0000),
#      3277: (-120.0000, -84.0000, -90.0000, -80.0000),
#      3278: (-90.0000, -84.0000, -60.0000, -80.0000),
#      3279: (-60.0000, -84.0000, -30.0000, -80.0000),
#      3280: (-30.0000, -84.0000, 0.0000, -80.0000),
#      3281: (0.0000, -84.0000, 30.0000, -80.0000),
#      3282: (30.0000, -84.0000, 60.0000, -80.0000),
#      3283: (60.0000, -84.0000, 90.0000, -80.0000),
#      3284: (90.0000, -84.0000, 120.0000, -80.0000),
#      3285: (120.0000, -84.0000, 150.0000, -80.0000),
#      3286: (150.0000, -84.0000, 180.0000, -80.0000),
#      3287: (-180.0000, -88.0000, -120.0000, -84.0000),
#      3288: (-120.0000, -88.0000, -60.0000, -84.0000),
#      3289: (-60.0000, -88.0000, 0.0000, -84.0000),
#      3290: (0.0000, -88.0000, 60.0000, -84.0000),
#      3291: (60.0000, -88.0000, 120.0000, -84.0000),
#      3292: (120.0000, -88.0000, 180.0000, -84.0000),
#      3293: (-180.0000, -90.0000, 180.0000, -88.0000),
#      3411: (-180.0000, 30.0000, 180.0000, 90.0000),
#      3412: (-180.0000, -90.0000, 180.0000, -40.0000),
#      3413: (-180.0000, 30.0000, 180.0000, 90.0000),
#      3995: (-180.0000, 60.0000, 180.0000, 90.0000),
#      3996: (-180.0000, 60.0000, 180.0000, 90.0000)}
#
#
# def is_polar_stereo(epsg: int) -> bool:
#     """Take an EPSG code, and return whether or not it's a polar-stereo projection.
#
#     This is useful to know
#     in case we're translating from geographic coordinates, which sometimes messes up bounds of certain datasets,
#     especially if they fall outside the bounds of the projection boundaries.
#
#     If it is polar-stereo, you can use the 'polar_stereo_epsg_bounds_bbox()' method to get the lon,lat bbox in which
#     this projection is defined and used. This can be helpful for subsetting data to only ones that are useful in that
#     projection."""
#     return get_horizontal_projection_only(epsg, as_epsg=True) in polar_stereo_bounds_dict
#
#
# def polar_stereo_espg_bounds_bbox(epsg: int, as_polygon=False):
#     """Given a polar stereo epsg, return the (minx, miny, maxx, maxy) bounding bot of WGS84 lon/lats in which that
#     projection is defined.
#
#     If as_polygon, return it as a polygon. Otherwise return it as a 4-item tuple.
#
#     If the horizontal component of the EPSG is not a defined polar stereo projection, raise a ValueError."""
#     epsg = get_horizontal_projection_only(epsg, as_epsg=True)
#     if not is_polar_stereo(epsg):
#         raise ValueError(f"Projection EPSG:{epsg} not defined as a polar stereo projection according to spatialreference.org")
#
#     bounds = polar_stereo_bounds_dict[epsg]
#
#     if as_polygon:
#         return shapely.geometry.box(*bounds)
#     else:
#         return bounds
