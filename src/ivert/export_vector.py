"""export_vector — convert IVERT NetCDF files to GIS vector formats.

Reads the .nc files produced by IS2Database._process_h5_to_nc() and writes them
as geolocated point vector files (GeoPackage, Shapefile, or CSV/XYZ), and reads
the IVERT database index .nc file and writes it as a polygon vector file, one
rectangular data_bbox footprint per granule.

This module is the library backing the 'ivert database export' command; it has
no command-line interface of its own.
"""

import logging
import os

import geopandas
import netCDF4
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_FORMATS = {
    "gpkg": ("GPKG", ".gpkg"),
    "shp": ("ESRI Shapefile", ".shp"),
    "csv": (None, ".csv"),
    "xyz": (None, ".xyz"),
}

WGS84_EPSG = 4326

# The two kinds of IVERT .nc file this module can export. Photon granules become
# point layers; the database index becomes a polygon layer.
KIND_PHOTONS = "photons"
KIND_INDEX = "index"

# Variables that identify each kind of .nc file (see detect_nc_kind()).
_GRANULE_SIGNATURE_VARS = ("x", "y", "z", "class_code")
_INDEX_SIGNATURE_VARS = ("filename", "granule_id", "data_bbox_xmin", "data_bbox_ymax")

# Shapefiles cap field names at 10 characters and silently truncate longer ones
# into collisions, so the index columns get explicit short aliases instead.
_INDEX_SHAPEFILE_ALIASES = {
    "source_granule": "src_gran",
    "horizontal_datum": "horiz_dtm",
    "vertical_datum": "vert_dtm",
    "query_bbox_xmin": "q_xmin",
    "query_bbox_xmax": "q_xmax",
    "query_bbox_ymin": "q_ymin",
    "query_bbox_ymax": "q_ymax",
    "query_bbox_tmin": "q_tmin",
    "query_bbox_tmax": "q_tmax",
    "data_bbox_xmin": "d_xmin",
    "data_bbox_xmax": "d_xmax",
    "data_bbox_ymin": "d_ymin",
    "data_bbox_ymax": "d_ymax",
    "data_bbox_tmin": "d_tmin",
    "data_bbox_tmax": "d_tmax",
    "zbounds_zmin": "zmin",
    "zbounds_zmax": "zmax",
    "numphotons_unclassified": "n_unclass",
    "numphotons_noise": "n_noise",
    "numphotons_ground": "n_ground",
    "numphotons_canopy": "n_canopy",
    "numphotons_canopy_top": "n_canoptop",
    "numphotons_ice_surface": "n_ice",
    "numphotons_bathy_floor": "n_bathyflr",
    "numphotons_bathy_surface": "n_bathysrf",
    "numphotons_buildings": "n_bldg",
    "numphotons_inland_water_surface": "n_inlwater",
    "downloaded_on_utc": "dnld_utc",
}


# ---------------------------------------------------------------------------
# File-kind detection
# ---------------------------------------------------------------------------
def detect_nc_kind(nc_path: str) -> str | None:
    """Identify what kind of IVERT NetCDF file this is.

    Returns KIND_INDEX for the database index file, KIND_PHOTONS for a photon
    granule file, or None if the file is a NetCDF file of neither kind (or isn't
    readable as NetCDF at all).
    """
    try:
        with netCDF4.Dataset(nc_path) as ds:
            names = set(ds.variables)
    except OSError:
        return None

    if all(v in names for v in _INDEX_SIGNATURE_VARS):
        return KIND_INDEX
    if all(v in names for v in _GRANULE_SIGNATURE_VARS):
        return KIND_PHOTONS
    return None


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------
def nc_to_geodataframe(
    nc_path: str,
    classes: list | None = None,
) -> geopandas.GeoDataFrame:
    """Read a single .nc granule file and return a GeoDataFrame.

    Args:
    nc_path : str
        Path to the .nc file.
    classes : list of int, optional
        If given, keep only photons whose class_code is in this list.

    """
    from ivert.photon_classes import class_names as _class_names

    class_names = _class_names()

    with netCDF4.Dataset(nc_path) as ds:

        def _arr(name):
            v = ds.variables[name][:]
            return v.data if hasattr(v, "data") else np.array(v)

        x = _arr("x")
        y = _arr("y")
        z = _arr("z").astype(float)
        class_code = _arr("class_code").astype(int)
        confidence = _arr("confidence").astype(int)
        delta_time = _arr("delta_time")

        bathy_conf = None
        if "bathy_confidence" in ds.variables:
            bathy_conf = _arr("bathy_confidence").astype(float)

        # Pull granule-level metadata from global attributes
        granule_id = getattr(
            ds,
            "granule_id",
            os.path.splitext(os.path.basename(nc_path))[0],
        )

    df = pd.DataFrame(
        {
            "x": x,
            "y": y,
            "z": z,
            "class_code": class_code,
            "class_name": pd.array(
                [class_names.get(c, f"class_{c}") for c in class_code],
                dtype="string",
            ),
            "confidence": confidence,
            "delta_time": delta_time,
            "granule_id": granule_id,
        },
    )
    if bathy_conf is not None:
        df["bathy_confidence"] = bathy_conf

    if classes is not None:
        df = df[df["class_code"].isin(classes)].reset_index(drop=True)

    if df.empty:
        return geopandas.GeoDataFrame(df, geometry=[], crs=WGS84_EPSG)

    geometry = geopandas.points_from_xy(df["x"], df["y"], df["z"])
    return geopandas.GeoDataFrame(df, geometry=geometry, crs=WGS84_EPSG)


def index_to_geodataframe(index_path: str) -> geopandas.GeoDataFrame:
    """Read an IVERT database index .nc file and return a polygon GeoDataFrame.

    Each row is one granule in the index, carrying every index field, with its
    data_bbox drawn as a rectangular polygon for the geometry.
    """
    import shapely.geometry

    from ivert.icesat2_database_v2 import IS2Database

    df = IS2Database.read_index_file(index_path)

    if len(df) == 0:
        return geopandas.GeoDataFrame(df, geometry=[], crs=WGS84_EPSG)

    geometry = [
        shapely.geometry.box(xmin, ymin, xmax, ymax)
        for xmin, xmax, ymin, ymax in zip(
            df["data_bbox_xmin"],
            df["data_bbox_xmax"],
            df["data_bbox_ymin"],
            df["data_bbox_ymax"],
            strict=True,
        )
    ]
    return geopandas.GeoDataFrame(df, geometry=geometry, crs=WGS84_EPSG)


def _prepare_for_shapefile(
    gdf: geopandas.GeoDataFrame,
    kind: str,
) -> geopandas.GeoDataFrame:
    """Return a copy of a GeoDataFrame whose fields fit the shapefile format.

    Shapefiles cap field names at 10 characters. Index fields are renamed to
    short aliases; photon fields that carry no per-point information worth the
    truncation (class_name, granule_id) are dropped.
    """
    if kind == KIND_INDEX:
        return gdf.rename(columns=_INDEX_SHAPEFILE_ALIASES)
    return gdf.drop(columns=["class_name", "granule_id"], errors="ignore")


def write_vector(
    gdf: geopandas.GeoDataFrame,
    outpath: str,
    fmt_key: str,
    overwrite: bool = False,
    kind: str = KIND_PHOTONS,
):
    """Write a GeoDataFrame to the requested vector format.

    kind is KIND_PHOTONS for a point layer of photons or KIND_INDEX for a
    polygon layer of granule footprints; it governs the shapefile field
    handling and the wording of the progress message.
    """
    if os.path.exists(outpath):
        if not overwrite:
            logger.info(
                "  Skipping existing %s (use -ow to overwrite).",
                os.path.basename(outpath),
            )
            return
        os.remove(outpath)

    driver, _ = SUPPORTED_FORMATS[fmt_key]

    if fmt_key in ("csv", "xyz"):
        out_df = gdf.drop(columns="geometry")
        out_df.to_csv(outpath, index=False)
    else:
        if fmt_key == "shp":
            gdf = _prepare_for_shapefile(gdf.copy(), kind)
        gdf.to_file(outpath, driver=driver)

    noun = "granules" if kind == KIND_INDEX else "photons"
    logger.info("  → %s  (%s %s)", outpath, f"{len(gdf):,}", noun)


# ---------------------------------------------------------------------------
# Multi-format helpers (used by 'ivert database export')
# ---------------------------------------------------------------------------
def normalize_format_keys(output_format: str, allowed=None) -> list:
    """Parse a comma-separated output-format string into a validated list of format keys.

    Args:
    output_format : str
        One format key or a comma-separated combination (e.g. "gpkg,shp,xyz").
    allowed : iterable of str, optional
        Restrict which format keys are accepted. Defaults to every key in
        SUPPORTED_FORMATS.

    Returns:
    list of str
        The format keys in the order given, with duplicates removed.

    Raises:
    ValueError
        If no format is given or an unsupported format is requested.

    """
    allowed = tuple(SUPPORTED_FORMATS) if allowed is None else tuple(allowed)

    keys = []
    for token in str(output_format).split(","):
        key = token.strip().lower().lstrip(".")
        if not key:
            continue
        if key not in allowed:
            raise ValueError(
                f"Unsupported output format '{token.strip()}'. "
                f"Choose from: {', '.join(allowed)}.",
            )
        if key not in keys:
            keys.append(key)

    if not keys:
        raise ValueError("No output format specified.")
    return keys


def subset_gdf_to_bbox(gdf: geopandas.GeoDataFrame, bbox) -> geopandas.GeoDataFrame:
    """Return the subset of a photon GeoDataFrame whose points fall within a bbox.

    bbox is (xmin, xmax, ymin, ymax) in the GeoDataFrame's own CRS. The maximum
    edges are exclusive, matching the IS2Database photon-query convention.
    """
    if gdf.empty:
        return gdf
    xmin, xmax, ymin, ymax = bbox
    x = gdf["x"]
    y = gdf["y"]
    mask = (x >= xmin) & (x < xmax) & (y >= ymin) & (y < ymax)
    return gdf[mask].reset_index(drop=True)


def subset_gdf_to_geometry(
    gdf: geopandas.GeoDataFrame,
    geometry,
) -> geopandas.GeoDataFrame:
    """Return the subset of a photon GeoDataFrame falling inside a polygon geometry.

    geometry is a single shapely (Multi)Polygon in the GeoDataFrame's own CRS,
    typically the union of every polygon in a user-supplied vector file. The
    geometry is prepared once and tested against all points in one vectorized
    call, so this stays fast for millions of photons.
    """
    if gdf.empty or geometry is None:
        return gdf

    import shapely

    shapely.prepare(geometry)
    mask = shapely.intersects(geometry, gdf.geometry.values)
    return gdf[mask].reset_index(drop=True)


def subset_gdf_to_date_range(
    gdf: geopandas.GeoDataFrame,
    dt_min: float,
    dt_max: float,
) -> geopandas.GeoDataFrame:
    """Return the subset of a photon GeoDataFrame within a delta_time range.

    dt_min and dt_max are ICESat-2 delta_time values (seconds since 2018-01-01).
    The upper bound is exclusive, matching IS2Database.read_granule().
    """
    if gdf.empty or "delta_time" not in gdf.columns:
        return gdf
    dt = gdf["delta_time"]
    mask = (dt >= dt_min) & (dt < dt_max)
    return gdf[mask].reset_index(drop=True)


def output_path_for_format(out_base: str, fmt_key: str) -> str:
    """Build the output path for a format by giving out_base the format's extension.

    Any recognized vector extension already on out_base is stripped first, so a
    base of 'photons.gpkg' combined with the 'shp' format yields 'photons.shp'.
    """
    _, ext = SUPPORTED_FORMATS[fmt_key]
    stem = out_base
    for _driver, known_ext in SUPPORTED_FORMATS.values():
        if stem.lower().endswith(known_ext):
            stem = stem[: -len(known_ext)]
            break
    return stem + ext


def write_vector_multi(
    gdf: geopandas.GeoDataFrame,
    out_base: str,
    fmt_keys,
    overwrite: bool = False,
    kind: str = KIND_PHOTONS,
) -> list:
    """Write a GeoDataFrame to one or more vector formats.

    Returns the list of file paths written (skipped existing files are omitted).
    """
    written = []
    for fmt_key in fmt_keys:
        outpath = output_path_for_format(out_base, fmt_key)
        existed = os.path.exists(outpath)
        write_vector(gdf, outpath, fmt_key, overwrite=overwrite, kind=kind)
        # write_vector() skips silently when the file exists and overwrite is False.
        if not existed or overwrite:
            written.append(outpath)
    return written
