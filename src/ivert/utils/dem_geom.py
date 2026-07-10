# -*- coding: utf-8 -*-
"""Geometry and projection helpers for reading DEM metadata.

These functions were originally part of icesat2_query.py but have no dependency on
the (deprecated) cudem library and are general-purpose enough to live in utils.
"""

import os
import pyproj
import rasterio
import rasterio.crs
import shapely
import shapely.geometry
import typing


def get_dem_reference_frame_from_user_input(
        crs: typing.Union[pyproj.CRS, "rasterio.crs.CRS", str, int, None],
        vert_horz_or_both: str = "both") -> typing.Union[pyproj.CRS, tuple, None]:
    """Return the horizontal and/or vertical CRS derived from an input CRS value.

    Parameters:
        crs: A CRS expressed as a pyproj.CRS, rasterio.crs.CRS, WKT string, EPSG int, or None.
        vert_horz_or_both: 'h' → horizontal only, 'v' → vertical only, 'b' → both (default).

    Returns:
        pyproj.CRS, a (horz, vert) tuple of pyproj.CRS, or None when unresolvable.

    Raises:
        ValueError: if vert_horz_or_both is not 'h', 'v', or 'b'.
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
        horz, vert = crs_obj, crs_obj
    elif crs_obj.is_vertical:
        horz, vert = None, crs_obj
    else:
        horz, vert = crs_obj, None

    choice_letter = vert_horz_or_both.strip().lower()[0]
    if choice_letter == "b":
        return horz, vert
    elif choice_letter == "h":
        return horz
    elif choice_letter == "v":
        return vert
    else:
        raise ValueError(
            f"Unknown choice '{vert_horz_or_both}' for vert_horz_or_both. "
            "Must begin with 'h', 'v', or 'b'.")


def get_dem_reference_frame_from_file(
        dem_fname: str,
        vert_horz_or_both: str = "both") -> typing.Union[pyproj.CRS, tuple, None]:
    """Read the CRS embedded in a raster file and return horizontal/vertical components.

    Parameters:
        dem_fname: Path to the raster file.
        vert_horz_or_both: 'h' → horizontal, 'v' → vertical, 'b' → both (default).

    Returns:
        pyproj.CRS, a (horz, vert) tuple, or None when unresolvable.

    Raises:
        FileNotFoundError: if the file does not exist.
    """
    if not os.path.exists(dem_fname):
        raise FileNotFoundError(f"DEM file {dem_fname} does not exist.")

    dem_ds = rasterio.open(dem_fname)
    dem_crs_str = "" if dem_ds.crs is None else dem_ds.crs
    return get_dem_reference_frame_from_user_input(dem_crs_str, vert_horz_or_both)


def get_dem_srs_string(horz_reference: pyproj.CRS,
                       vert_reference: pyproj.CRS) -> str:
    """Build a compound SRS string like 'EPSG:4326+3855' from separate H+V pyproj.CRS objects.

    Raises:
        ValueError: if the two datums are based on different authorities.

    Returns:
        String in the format 'AUTH:HORZ+VERT', or just the horz SRS if both axes are identical.
    """
    horz_auth = horz_reference.list_authority()[0].auth_name.upper()
    vert_auth = vert_reference.list_authority()[0].auth_name.upper()

    if horz_auth != vert_auth:
        raise ValueError(
            "Reference authorities for the horizontal and vertical datums must match.")

    if horz_reference.equals(vert_reference):
        return horz_reference.srs
    return f"{horz_auth}:{horz_reference.list_authority()[0].code}+{vert_reference.list_authority()[0].code}"


def get_wgs84_bounding_box(
        polygon_bbox_or_dem_fname: typing.Union[shapely.geometry.Polygon,
                                                list, tuple, str],
        dem_horz_reference_frame: typing.Union[str, pyproj.CRS, None] = None
) -> tuple:
    """Return a 4-tuple (xmin, xmax, ymin, ymax) in WGS84 from a DEM file, bbox, or polygon.

    Parameters:
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
            dem_horz_reference_frame, "horz")

    elif type(polygon_bbox_or_dem_fname) in (list, tuple):
        bbox = polygon_bbox_or_dem_fname
        if len(bbox) == 4:
            # Convert (xmin, xmax, ymin, ymax) → shapely box expects (xmin, ymin, xmax, ymax)
            polygon = shapely.geometry.box(bbox[0], bbox[2], bbox[1], bbox[3])
        elif len(bbox) > 4 and len(bbox) % 2 == 0:
            polygon = shapely.geometry.Polygon(bbox)
        else:
            raise TypeError(
                "polygon_bbox_or_dem_fname as a list/tuple must be a 4-value "
                "(xmin, xmax, ymin, ymax) bbox or an even-length coordinate sequence.")
        dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
            dem_horz_reference_frame, "horz")

    elif isinstance(polygon_bbox_or_dem_fname, str):
        if not os.path.exists(polygon_bbox_or_dem_fname):
            raise FileNotFoundError(
                f"File not found: {polygon_bbox_or_dem_fname}")
        if dem_horz_reference_frame is None:
            dem_horz_reference_frame = get_dem_reference_frame_from_file(
                polygon_bbox_or_dem_fname, "horz")
        else:
            dem_horz_reference_frame = get_dem_reference_frame_from_user_input(
                dem_horz_reference_frame, "horz")
        bbox = rasterio.open(polygon_bbox_or_dem_fname).bounds
        # rasterio bounds are (left, bottom, right, top) = (xmin, ymin, xmax, ymax)
        polygon = shapely.geometry.box(*bbox)

    else:
        raise TypeError(
            "polygon_bbox_or_dem_fname must be a filename string, a 4-item bbox, "
            "or a shapely Polygon.")

    if dem_horz_reference_frame is None:
        raise ValueError("dem_horz_reference_frame could not be resolved.")

    assert isinstance(polygon, shapely.geometry.Polygon)
    assert isinstance(dem_horz_reference_frame, pyproj.CRS)
    assert not dem_horz_reference_frame.is_compound

    wgs84_crs = pyproj.CRS.from_user_input("EPSG:4326")

    if dem_horz_reference_frame.equals(wgs84_crs):
        b = polygon.bounds  # (xmin, ymin, xmax, ymax)
        return b[0], b[2], b[1], b[3]  # → (xmin, xmax, ymin, ymax)

    transformer = pyproj.Transformer.from_crs(
        dem_horz_reference_frame, wgs84_crs, always_xy=True)
    polygon_wgs84 = shapely.geometry.Polygon(
        shell=transformer.itransform(polygon.exterior.coords[:]))

    b = polygon_wgs84.bounds  # (xmin, ymin, xmax, ymax)
    return b[0], b[2], b[1], b[3]  # → (xmin, xmax, ymin, ymax)
