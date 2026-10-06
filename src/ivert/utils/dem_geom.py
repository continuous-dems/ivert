"""Geometry and projection helpers for reading DEM metadata.

These functions were originally part of icesat2_query.py but have no dependency on
the (deprecated) cudem library and are general-purpose enough to live in utils.
"""

import logging
import os
import typing

import pyproj
import rasterio
import rasterio.crs
import shapely
import shapely.geometry

from ivert.utils import dem_source

logger = logging.getLogger(__name__)

# OGC's longitude-first versions of geographic CRSs, which GDAL reports for some
# formats (ASCII Grid, ESRI .hdr/.flt). pyproj finds no EPSG match for them, since
# their axis order differs, but IVERT always transforms in x/y (lon/lat) order.
_OGC_LON_LAT_TO_EPSG = {"CRS84": 4326, "CRS83": 4269, "CRS27": 4267}


def _epsg_equivalent(crs: pyproj.CRS | None) -> pyproj.CRS | None:
    """Return the EPSG version of a CRS where one exists, so it pairs with EPSG datums."""
    if crs is None:
        return None
    authority = crs.list_authority()
    if authority and authority[0].auth_name.upper() == "EPSG":
        return crs
    if authority and authority[0].auth_name.upper() == "OGC":
        code = _OGC_LON_LAT_TO_EPSG.get(authority[0].code.upper())
        if code is not None:
            return pyproj.CRS.from_epsg(code)
    found = crs.to_authority("EPSG")
    return crs if found is None else pyproj.CRS.from_epsg(int(found[1]))


def get_dem_reference_frame_from_user_input(
    crs: typing.Union[pyproj.CRS, "rasterio.crs.CRS", str, int, None],
    vert_horz_or_both: str = "both",
) -> pyproj.CRS | tuple | None:
    """Return the horizontal and/or vertical CRS derived from an input CRS value.

    Args:
        crs: A CRS expressed as a pyproj.CRS, rasterio.crs.CRS, WKT string, EPSG int, or None.
        vert_horz_or_both: 'h' → horizontal only, 'v' → vertical only, 'b' → both (default).

    Returns:
        pyproj.CRS, a (horz, vert) tuple of pyproj.CRS, or None when unresolvable.

    Raises:
        ValueError: if vert_horz_or_both is not 'h', 'v', or 'b'.

    """
    if crs is None or crs == "":
        crs_obj = None
    elif isinstance(crs, (rasterio.crs.CRS, pyproj.CRS)):
        crs_obj = pyproj.CRS(crs)
    else:
        crs_obj = pyproj.CRS.from_user_input(crs)

    if crs_obj is None:
        horz, vert = None, None
    elif crs_obj.is_compound:
        horz, vert = crs_obj.sub_crs_list
    elif len(crs_obj.axis_info) == 3:
        horz, vert = crs_obj, crs_obj
    elif crs_obj.is_vertical:
        horz, vert = None, crs_obj
    else:
        horz, vert = crs_obj, None
    # A 3D CRS is its own vertical reference, so leave it as it is.
    if horz is not vert:
        horz = _epsg_equivalent(horz)

    choice_letter = vert_horz_or_both.strip().lower()[0]
    if choice_letter == "b":
        return horz, vert
    if choice_letter == "h":
        return horz
    if choice_letter == "v":
        return vert
    msg = (
        f"Unknown choice '{vert_horz_or_both}' for vert_horz_or_both. "
        "Must begin with 'h', 'v', or 'b'."
    )
    raise ValueError(msg)


def get_dem_reference_frame_from_file(
    dem_fname: str,
    vert_horz_or_both: str = "both",
) -> pyproj.CRS | tuple | None:
    """Read the CRS embedded in a raster file and return horizontal/vertical components.

    Args:
        dem_fname: Path to the raster file.
        vert_horz_or_both: 'h' → horizontal, 'v' → vertical, 'b' → both (default).

    Returns:
        pyproj.CRS, a (horz, vert) tuple, or None when unresolvable.

    Raises:
        FileNotFoundError: if the file does not exist.

    """
    if not os.path.exists(dem_source.dem_file_path(dem_fname)):
        msg = f"DEM file {dem_fname} does not exist."
        raise FileNotFoundError(msg)

    dem_ds = rasterio.open(dem_fname)
    dem_crs_str = "" if dem_ds.crs is None else dem_ds.crs
    return get_dem_reference_frame_from_user_input(dem_crs_str, vert_horz_or_both)


def get_dem_srs_string(
    horz_reference: pyproj.CRS,
    vert_reference: pyproj.CRS | str,
) -> str:
    """Build a compound SRS string like 'EPSG:4326+3855' from a horizontal and a vertical reference.

    Args:
        horz_reference: The horizontal CRS.
        vert_reference: The vertical CRS, or a transformez reference ID such as
            'vdatum:mllw', which pyproj cannot represent.

    Raises:
        ValueError: if the two datums are based on different authorities.

    Returns:
        String in the format 'AUTH:HORZ+VERT' (or 'AUTH:HORZ+vdatum:mllw'), or just
        the horz SRS if both axes are identical.

    """
    horz_auth = horz_reference.list_authority()[0].auth_name.upper()
    if isinstance(vert_reference, str):
        return f"{horz_auth}:{horz_reference.list_authority()[0].code}+{vert_reference}"
    vert_auth = vert_reference.list_authority()[0].auth_name.upper()

    if horz_auth != vert_auth:
        msg = "Reference authorities for the horizontal and vertical datums must match."
        raise ValueError(msg)

    if horz_reference.equals(vert_reference):
        return horz_reference.srs
    return f"{horz_auth}:{horz_reference.list_authority()[0].code}+{vert_reference.list_authority()[0].code}"


def split_srs_string(
    srs: str | int | pyproj.CRS,
) -> tuple[pyproj.CRS | None, str | None]:
    """Split an SRS into its horizontal CRS and its vertical reference.

    Args:
        srs: A CRS pyproj can read ('EPSG:4326+3855', 4979, a pyproj.CRS), or a
            compound string whose vertical part is a transformez reference ID
            ('EPSG:4326+vdatum:mllw'), or a vertical reference ID on its own.

    Returns:
        (horizontal, vertical). The horizontal part is a pyproj.CRS or None. The
        vertical part is a bare EPSG code ('3855'), a transformez reference ID as
        given ('vdatum:mllw'), or None. A 3D geographic CRS such as EPSG:4979 is
        both: its own code is its vertical reference.
    """
    try:
        crs = pyproj.CRS.from_user_input(srs)
    except pyproj.exceptions.CRSError:
        text = str(srs).strip()
        if "+" not in text:
            return None, text
        horizontal, vertical = text.rsplit("+", 1)
        return _epsg_equivalent(pyproj.CRS.from_user_input(horizontal)), vertical

    def code(c: pyproj.CRS | None) -> str | None:
        epsg = None if c is None else c.to_epsg()
        return None if epsg is None else str(epsg)

    if crs.is_compound:
        vert = next((s for s in crs.sub_crs_list if s.is_vertical), None)
        horz = next((s for s in crs.sub_crs_list if not s.is_vertical), None)
        return _epsg_equivalent(horz), code(vert)
    if crs.is_vertical:
        return None, code(crs)
    # pyproj marks a 3D geographic CRS (e.g. EPSG:4979, WGS84 with ellipsoidal
    # height) as neither compound nor vertical.
    if crs.is_geographic and len(crs.axis_info) == 3:
        return crs, code(crs)
    return _epsg_equivalent(crs), None


def get_wgs84_bounding_box(
    polygon_bbox_or_dem_fname: shapely.geometry.Polygon | list | tuple | str,
    dem_horz_reference_frame: str | pyproj.CRS | None = None,
) -> tuple:
    """Return a 4-tuple (xmin, xmax, ymin, ymax) in WGS84 from a DEM file, bbox, or polygon.

    Args:
        polygon_bbox_or_dem_fname:
            - A filename string → CRS is read from the file.
            - A 4-item (xmin, xmax, ymin, ymax) list/tuple.
            - A shapely Polygon.
        dem_horz_reference_frame: Override the horizontal CRS (string, int, or pyproj.CRS).
            Required when passing a bbox or polygon; optional (overrides file CRS) for filenames.

    Returns:
        (xmin, xmax, ymin, ymax) in WGS84 (EPSG:4326).

    Raises:
        ValueError: if dem_horz_reference_frame cannot be resolved.
        FileNotFoundError: if a filename is given but does not exist.
        TypeError: if the input type is unhandled.

    """
    polygon = None

    if isinstance(polygon_bbox_or_dem_fname, shapely.geometry.Polygon):
        polygon = shapely.Polygon(polygon_bbox_or_dem_fname.exterior.coords[:])
        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame,
            "horz",
        )

    elif type(polygon_bbox_or_dem_fname) in (list, tuple):
        bbox = polygon_bbox_or_dem_fname
        if len(bbox) == 4:
            # Convert (xmin, xmax, ymin, ymax) → shapely box expects (xmin, ymin, xmax, ymax)
            polygon = shapely.geometry.box(bbox[0], bbox[2], bbox[1], bbox[3])
        elif len(bbox) > 4 and len(bbox) % 2 == 0:
            polygon = shapely.geometry.Polygon(bbox)
        else:
            msg = (
                "polygon_bbox_or_dem_fname as a list/tuple must be a 4-value "
                "(xmin, xmax, ymin, ymax) bbox or an even-length coordinate sequence."
            )
            raise TypeError(msg)
        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame,
            "horz",
        )

    elif isinstance(polygon_bbox_or_dem_fname, str):
        if not os.path.exists(dem_source.dem_file_path(polygon_bbox_or_dem_fname)):
            msg = f"File not found: {polygon_bbox_or_dem_fname}"
            raise FileNotFoundError(msg)
        if dem_horz_reference_frame is None:
            dem_horz_reference_frame = get_dem_reference_frame_from_file(
                polygon_bbox_or_dem_fname,
                "horz",
            )
        else:
            dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
                dem_horz_reference_frame,
                "horz",
            )
        bbox = rasterio.open(polygon_bbox_or_dem_fname).bounds
        # rasterio bounds are (left, bottom, right, top) = (xmin, ymin, xmax, ymax)
        polygon = shapely.geometry.box(*bbox)

    else:
        msg = (
            "polygon_bbox_or_dem_fname must be a filename string, a 4-item bbox, "
            "or a shapely Polygon."
        )
        raise TypeError(msg)

    if dem_horz_reference_frame is None:
        msg = "dem_horz_reference_frame could not be resolved."
        raise ValueError(msg)

    assert isinstance(polygon, shapely.geometry.Polygon)
    assert isinstance(dem_horz_reference_frame, pyproj.CRS)
    assert not dem_horz_reference_frame.is_compound

    wgs84_crs = pyproj.CRS.from_user_input("EPSG:4326")

    if dem_horz_reference_frame.equals(wgs84_crs):
        b = polygon.bounds  # (xmin, ymin, xmax, ymax)
        return b[0], b[2], b[1], b[3]  # → (xmin, xmax, ymin, ymax)

    transformer = pyproj.Transformer.from_crs(
        dem_horz_reference_frame,
        wgs84_crs,
        always_xy=True,
    )
    polygon_wgs84 = shapely.geometry.Polygon(
        shell=transformer.itransform(polygon.exterior.coords[:]),
    )

    b = polygon_wgs84.bounds  # (xmin, ymin, xmax, ymax)
    return b[0], b[2], b[1], b[3]  # → (xmin, xmax, ymin, ymax)


def _comparable_reference(reference):
    """Return a CRS or reference ID in a form that can be compared for equality."""
    if isinstance(reference, pyproj.CRS):
        return reference
    try:
        return pyproj.CRS.from_user_input(reference)
    except pyproj.exceptions.CRSError:
        # A transformez reference ID such as 'vdatum:mllw', which pyproj can't read.
        return str(reference).strip().lower()


def same_reference(a, b):
    """Return True if two CRSs or vertical reference IDs name the same thing."""
    a, b = _comparable_reference(a), _comparable_reference(b)
    if isinstance(a, pyproj.CRS) and isinstance(b, pyproj.CRS):
        return a.equals(b)
    return a == b


def reference_label(reference):
    """Return a short printable name for a CRS or vertical reference ID."""
    if isinstance(reference, pyproj.CRS):
        return reference.to_string()
    return str(reference)


def resolve_horizontal_crs(dem_name, file_horz_crs, projection_horz=None):
    """Return a DEM's horizontal CRS: the one from -p/--projection, else the file's.

    Args:
        dem_name: The DEM's path or subdataset string, for messages.
        file_horz_crs: The horizontal CRS in the DEM file, or None if it has none.
        projection_horz: The horizontal part of the user's -p/--projection, or None.

    A warning is logged when -p/--projection replaces a different CRS in the file.

    Raises:
        ValueError: if neither the file nor -p/--projection gives a CRS.

    """
    if projection_horz is None:
        if file_horz_crs is None:
            msg = (
                f"{dem_source.dem_file_path(dem_name)} has no coordinate reference system. "
                "Use -p/--projection to give one."
            )
            raise ValueError(msg)
        return file_horz_crs
    if file_horz_crs is not None and not same_reference(projection_horz, file_horz_crs):
        logger.warning(
            "Using -p/--projection %s for %s in place of the CRS in the file (%s).",
            reference_label(projection_horz),
            os.path.basename(dem_source.dem_file_path(dem_name)),
            reference_label(file_horz_crs),
        )
    return projection_horz
