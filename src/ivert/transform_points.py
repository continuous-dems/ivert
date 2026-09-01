import logging
import math
import os

import numpy as np
import pyproj
import rasterio
import transformez

logger = logging.getLogger(__name__)

_GRID_RESOLUTION = "3s"  # ~90 m — appropriate resolution for datum shift grids

# Shift grids are generated over the requested region snapped outward to a multiple
# of this many degrees. The cache file name carries the snapped bounds, so the name
# describes exactly the extent the grid covers and any later request landing inside
# that box is genuinely covered by the cached file.
_GRID_SNAP_DEGREES = 0.1


def transform_points(
    x: list | tuple | np.ndarray,
    y: list | tuple | np.ndarray,
    z: list | tuple | np.ndarray,
    src_epsg: str | int,
    dst_epsg: str | int,
    src_region: list | tuple | np.ndarray | None = None,
    cache_dir: str | None = None,
) -> tuple:
    """Transform a set of 3D points from one coordinate reference system to another.

    Parameters
    ----------
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

    Raises
    ------
        ValueError: If the vertical datum transformation cannot be built.

    Returns
    -------
        A 3-tuple of (x, y, z) numpy arrays in the destination CRS.

    """
    src_crs = pyproj.CRS.from_user_input(src_epsg)
    dst_crs = pyproj.CRS.from_user_input(dst_epsg)

    if src_crs.is_exact_same(dst_crs):
        return x, y, z

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    z = np.asarray(z, dtype=float)

    src_horz, src_vert_epsg = _decompose_crs(src_crs)
    dst_horz, dst_vert_epsg = _decompose_crs(dst_crs)

    # Horizontal reprojection
    if (
        src_horz is not None
        and dst_horz is not None
        and not src_horz.is_exact_same(dst_horz)
    ):
        xformer = pyproj.Transformer.from_crs(src_horz, dst_horz, always_xy=True)
        trans_x, trans_y = xformer.transform(x, y)
    else:
        trans_x, trans_y = x.copy(), y.copy()

    # Vertical datum shift
    if (
        src_vert_epsg is not None
        and dst_vert_epsg is not None
        and src_vert_epsg != dst_vert_epsg
    ):
        trans_z = _apply_vertical_transform(
            x,
            y,
            z,
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
) -> tuple[pyproj.CRS | None, int | None]:
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


def _snap_region_outward(
    region_bounds: list[float],
    snap: float = _GRID_SNAP_DEGREES,
) -> list[float]:
    """Expand [w, e, s, n] outward to the surrounding multiples of 'snap' degrees.

    Snapping makes the region a function of a coarse grid rather than of the exact
    input points, so runs over slightly different extents share one cached shift
    grid instead of colliding on a rounded file name while needing different areas.
    """
    w, e, s, n = region_bounds
    w_snap = math.floor(w / snap) * snap
    e_snap = math.ceil(e / snap) * snap
    s_snap = math.floor(s / snap) * snap
    n_snap = math.ceil(n / snap) * snap

    # A bound sitting exactly on a multiple can snap to itself on both sides (and
    # floating-point division makes that hard to predict), which would ask for a
    # zero-width or zero-height grid. Keep at least one snap unit in each dimension.
    if e_snap - w_snap < snap:
        e_snap = w_snap + snap
    if n_snap - s_snap < snap:
        n_snap = s_snap + snap

    return [w_snap, e_snap, s_snap, n_snap]


def _grid_covers_region(grid_fn: str, region_bounds: list[float], tol=1e-9) -> bool:
    """Return True if the raster at 'grid_fn' is readable and spans region_bounds."""
    w, e, s, n = region_bounds
    try:
        with rasterio.open(grid_fn) as src:
            bounds = src.bounds
    except (OSError, rasterio.errors.RasterioError):
        # Unreadable or truncated (e.g. an interrupted earlier run): treat as absent.
        return False

    return (
        (bounds.left <= w + tol)
        and (bounds.right >= e - tol)
        and (bounds.bottom <= s + tol)
        and (bounds.top >= n - tol)
    )


def _apply_vertical_transform(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    src_vert_epsg: str,
    dst_vert_epsg: str,
    src_region: list | tuple | np.ndarray | None,
    cache_dir: str | None,
) -> np.ndarray:
    """Compute and apply a vertical datum shift to z via a cached transformez grid."""
    from scipy.interpolate import RegularGridInterpolator

    if src_region is None:
        region_bounds = [float(x.min()), float(x.max()), float(y.min()), float(y.max())]
    else:
        region_bounds = [
            float(src_region[0]),
            float(src_region[1]),
            float(src_region[2]),
            float(src_region[3]),
        ]

    _cache = cache_dir or os.path.join(os.getcwd(), "transformez_cache")
    os.makedirs(_cache, exist_ok=True)

    # Strip any "EPSG:" and/or any compound ("4326+4979") datum strings fed to this
    # function. Done before the file name is built so the name never carries a ':'
    # (which is not a legal path character on Windows) or a '+'.
    src_vert_epsg = src_vert_epsg.rsplit(":", maxsplit=1)[-1].rsplit("+", maxsplit=1)[
        -1
    ]
    dst_vert_epsg = dst_vert_epsg.rsplit(":", maxsplit=1)[-1].rsplit("+", maxsplit=1)[
        -1
    ]

    grid_region = _snap_region_outward(region_bounds)
    w, e, s, n = grid_region
    grid_fn = os.path.join(
        _cache,
        f"vshift_{src_vert_epsg}_{dst_vert_epsg}_{w:.1f}_{e:.1f}_{s:.1f}_{n:.1f}.tif",
    )

    # Never reuse a cached grid that does not actually span the points being
    # transformed. Grids written by earlier versions were named from bounds rounded
    # to 0.1 degrees but generated over the exact (smaller) region, so a later run
    # covering more ground would silently reuse one that fell short and leave the
    # uncovered points unconverted. Coverage is checked against the real data
    # region, not the snapped one, so a grid trimmed by a pixel of increment
    # rounding is still accepted.
    if os.path.exists(grid_fn) and not _grid_covers_region(grid_fn, region_bounds):
        logger.info(
            "Cached shift grid %s does not cover the requested region; regenerating.",
            grid_fn,
        )
        os.remove(grid_fn)

    if not os.path.exists(grid_fn):
        shift_array = transformez.generate_grid(
            region=grid_region,
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
                f"over region {grid_region}.",
            )

    with rasterio.open(grid_fn) as src:
        shift_data = src.read(1).astype(float)
        grid_bounds = src.bounds
        if src.nodata is not None:
            shift_data[np.isclose(shift_data, src.nodata, atol=1e-4)] = np.nan

    height, width = shift_data.shape
    lons = np.linspace(grid_bounds.left, grid_bounds.right, width)
    lats = np.linspace(grid_bounds.bottom, grid_bounds.top, height)

    # A point outside the grid must never come back as a 0.0 shift: an unconverted
    # height is indistinguishable from a legitimate zero separation, and silently
    # leaves those points wrong by the full datum offset (~24 m for NAVD88 on the
    # Oregon coast). Fail loudly instead. The coverage check above should make this
    # unreachable; it is the backstop if it ever is not.
    outside = (
        (x < grid_bounds.left)
        | (x > grid_bounds.right)
        | (y < grid_bounds.bottom)
        | (y > grid_bounds.top)
    )
    if outside.any():
        n_outside = int(np.count_nonzero(outside))
        raise ValueError(
            f"Vertical transform failed: {n_outside:,} of {outside.size:,} points fall "
            f"outside the shift grid {grid_fn} "
            f"(grid covers {tuple(round(b, 6) for b in grid_bounds)}, points span "
            f"x [{float(x.min()):.6f}, {float(x.max()):.6f}], "
            f"y [{float(y.min()):.6f}, {float(y.max()):.6f}]). "
            "Delete that file and retry.",
        )

    # rasterio stores rows top-to-bottom; flip to ascending-lat order for interpolator
    interp = RegularGridInterpolator(
        (lats, lons),
        shift_data[::-1, :],
        method="linear",
        bounds_error=False,
        fill_value=np.nan,
    )

    shifts = interp(np.column_stack([y, x]))

    # In-bounds NaNs mean the grid itself has nodata there (a genuine gap in the
    # datum model), not a caching problem. Those heights cannot be converted, so
    # they stay NaN and are dropped downstream rather than silently passed through.
    n_nan = int(np.count_nonzero(np.isnan(shifts)))
    if n_nan:
        logger.warning(
            "%d of %d points fall on nodata cells of shift grid %s; "
            "their transformed heights will be NaN.",
            n_nan,
            shifts.size,
            grid_fn,
        )

    return z + shifts
