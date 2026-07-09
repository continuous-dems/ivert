import os
import logging
import numpy
import pyproj
import rasterio
import typing

import transformez

logger = logging.getLogger(__name__)

_GRID_RESOLUTION = "3s"  # ~90 m — appropriate resolution for datum shift grids


def transform_points(
    x: typing.Union[list, tuple, numpy.ndarray],
    y: typing.Union[list, tuple, numpy.ndarray],
    z: typing.Union[list, tuple, numpy.ndarray],
    src_epsg: typing.Union[str, int],
    dst_epsg: typing.Union[str, int],
    src_region: typing.Union[list, tuple, numpy.ndarray, None] = None,
    cache_dir: typing.Optional[str] = None,
) -> tuple:
    """Transform a set of 3D points from one coordinate reference system to another.

    Parameters:
        x: X-coordinates (longitude or easting).
        y: Y-coordinates (latitude or northing).
        z: Z-coordinates (elevation).
        src_epsg: Source CRS as an EPSG code (int or str) or compound string
            (e.g. "EPSG:4326+3855" for WGS84 horizontal + EGM2008 vertical).
        dst_epsg: Destination CRS in the same formats as src_epsg.
        src_region: Bounding box [xmin, xmax, ymin, ymax] in the source CRS.
            If None, derived from the extents of the input points.
        cache_dir: Directory for caching downloaded datum grids and the
            generated vertical shift grids. Defaults to './transformez_cache'
            in the current working directory.

    Creates:
        Shift grid .tif files may be written to cache_dir. These are reused
        on subsequent calls covering the same datum pair and region.

    Raises:
        ValueError: If the vertical datum transformation cannot be built.

    Returns:
        A 3-tuple of (x, y, z) numpy arrays in the destination CRS.
    """
    src_crs = pyproj.CRS.from_user_input(src_epsg)
    dst_crs = pyproj.CRS.from_user_input(dst_epsg)

    if src_crs.is_exact_same(dst_crs):
        return x, y, z

    x = numpy.asarray(x, dtype=float)
    y = numpy.asarray(y, dtype=float)
    z = numpy.asarray(z, dtype=float)

    src_horz, src_vert_epsg = _decompose_crs(src_crs)
    dst_horz, dst_vert_epsg = _decompose_crs(dst_crs)

    # Horizontal reprojection
    if src_horz is not None and dst_horz is not None and not src_horz.is_exact_same(dst_horz):
        xformer = pyproj.Transformer.from_crs(src_horz, dst_horz, always_xy=True)
        trans_x, trans_y = xformer.transform(x, y)
    else:
        trans_x, trans_y = x.copy(), y.copy()

    # Vertical datum shift
    if src_vert_epsg is not None and dst_vert_epsg is not None and src_vert_epsg != dst_vert_epsg:
        trans_z = _apply_vertical_transform(
            x, y, z,
            src_vert_epsg=str(src_vert_epsg),
            dst_vert_epsg=str(dst_vert_epsg),
            src_region=src_region,
            cache_dir=cache_dir,
        )
    else:
        trans_z = z.copy()

    return trans_x, trans_y, trans_z


def _decompose_crs(
    crs: pyproj.CRS,
) -> typing.Tuple[typing.Optional[pyproj.CRS], typing.Optional[int]]:
    """Return (horizontal_crs, vertical_epsg) from a possibly compound CRS."""
    if crs.is_compound:
        vert = next((s for s in crs.sub_crs_list if s.is_vertical), None)
        horz = next((s for s in crs.sub_crs_list if not s.is_vertical), None)
        return horz, (vert.to_epsg() if vert else None)
    if crs.is_vertical:
        return None, crs.to_epsg()
    # 3D geographic CRS (e.g. EPSG:4979 = WGS84 3D with ellipsoidal height).
    # pyproj does not mark these as compound or vertical, so extract the EPSG
    # directly and treat it as the vertical datum identifier.
    if crs.is_geographic and len(crs.axis_info) == 3:
        return crs, crs.to_epsg()
    return crs, None


def _apply_vertical_transform(
    x: numpy.ndarray,
    y: numpy.ndarray,
    z: numpy.ndarray,
    src_vert_epsg: str,
    dst_vert_epsg: str,
    src_region: typing.Union[list, tuple, numpy.ndarray, None],
    cache_dir: typing.Optional[str],
) -> numpy.ndarray:
    """Compute and apply a vertical datum shift to z via a cached transformez grid."""
    from scipy.interpolate import RegularGridInterpolator

    if src_region is None:
        region_bounds = [float(x.min()), float(x.max()), float(y.min()), float(y.max())]
    else:
        region_bounds = [float(src_region[0]), float(src_region[1]),
                         float(src_region[2]), float(src_region[3])]

    _cache = cache_dir or os.path.join(os.getcwd(), "transformez_cache")
    os.makedirs(_cache, exist_ok=True)

    w, e, s, n = region_bounds
    grid_fn = os.path.join(
        _cache,
        f"vshift_{src_vert_epsg}_{dst_vert_epsg}_{w:.1f}_{e:.1f}_{s:.1f}_{n:.1f}.tif",
    )

    # Strip any "EPSG:" and/or any compound ("4326+4979") datum strings fed to this function.
    src_vert_epsg = src_vert_epsg.split(":")[-1].split("+")[-1]
    dst_vert_epsg = dst_vert_epsg.split(":")[-1].split("+")[-1]

    if not os.path.exists(grid_fn):
        shift_array = transformez.generate_grid(
            region=region_bounds,
            increment=_GRID_RESOLUTION,
            datum_in=src_vert_epsg,
            datum_out=dst_vert_epsg,
            cache_dir=_cache,
            out_fn=grid_fn,
            verbose=False,
        )
        if shift_array is None:
            raise ValueError(
                f"Vertical transform failed: EPSG:{src_vert_epsg} → EPSG:{dst_vert_epsg} "
                f"over region {region_bounds}."
            )

    with rasterio.open(grid_fn) as src:
        shift_data = src.read(1).astype(float)
        grid_bounds = src.bounds
        if src.nodata is not None:
            shift_data[numpy.isclose(shift_data, src.nodata, atol=1e-4)] = numpy.nan

    height, width = shift_data.shape
    lons = numpy.linspace(grid_bounds.left, grid_bounds.right, width)
    lats = numpy.linspace(grid_bounds.bottom, grid_bounds.top, height)

    # rasterio stores rows top-to-bottom; flip to ascending-lat order for interpolator
    interp = RegularGridInterpolator(
        (lats, lons),
        shift_data[::-1, :],
        method="linear",
        bounds_error=False,
        fill_value=0.0,
    )

    shifts = interp(numpy.column_stack([y, x]))
    return z + shifts
