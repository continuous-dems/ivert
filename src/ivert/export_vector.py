#!/usr/bin/env python3
"""export_vector — convert IVERT ICESat-2 .nc granule files to GIS vector formats.

Reads .nc files produced by IS2Database._process_h5_to_nc() and writes them as
geolocated point vector files (GeoPackage, Shapefile, or CSV/XYZ).

This module is the library backing the 'ivert database export' command; it has
no command-line interface of its own.
"""

import os

import geopandas
import netCDF4
import numpy as np
import pandas as pd

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


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------
def nc_to_geodataframe(nc_path: str, classes: list = None) -> geopandas.GeoDataFrame:
    """Read a single .nc granule file and return a GeoDataFrame.

    Parameters
    ----------
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
    gdf = geopandas.GeoDataFrame(df, geometry=geometry, crs=WGS84_EPSG)
    return gdf


def write_vector(
    gdf: geopandas.GeoDataFrame,
    outpath: str,
    fmt_key: str,
    overwrite: bool = False,
):
    """Write a GeoDataFrame to the requested vector format."""
    if os.path.exists(outpath):
        if not overwrite:
            print(
                f"  Skipping existing {os.path.basename(outpath)} (use -ow to overwrite).",
            )
            return
        os.remove(outpath)

    driver, _ = SUPPORTED_FORMATS[fmt_key]

    if fmt_key in ("csv", "xyz"):
        out_df = gdf.drop(columns="geometry")
        out_df.to_csv(outpath, index=False)
    else:
        # Shapefiles truncate field names to 10 chars and don't support
        # string columns well — drop granule_id if it would be truncated badly.
        if fmt_key == "shp":
            gdf = gdf.copy()
            gdf = gdf.drop(columns=["class_name", "granule_id"], errors="ignore")
        gdf.to_file(outpath, driver=driver)

    print(f"  → {outpath}  ({len(gdf):,} photons)")


# ---------------------------------------------------------------------------
# Multi-format helpers (used by 'ivert database export')
# ---------------------------------------------------------------------------
def normalize_format_keys(output_format: str, allowed=None) -> list:
    """Parse a comma-separated output-format string into a validated list of format keys.

    Parameters
    ----------
    output_format : str
        One format key or a comma-separated combination (e.g. "gpkg,shp,xyz").
    allowed : iterable of str, optional
        Restrict which format keys are accepted. Defaults to every key in
        SUPPORTED_FORMATS.

    Returns
    -------
    list of str
        The format keys in the order given, with duplicates removed.

    Raises
    ------
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
) -> list:
    """Write a GeoDataFrame to one or more vector formats.

    Returns the list of file paths written (skipped existing files are omitted).
    """
    written = []
    for fmt_key in fmt_keys:
        outpath = output_path_for_format(out_base, fmt_key)
        existed = os.path.exists(outpath)
        write_vector(gdf, outpath, fmt_key, overwrite=overwrite)
        # write_vector() skips silently when the file exists and overwrite is False.
        if not existed or overwrite:
            written.append(outpath)
    return written
