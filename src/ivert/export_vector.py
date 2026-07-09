#!/usr/bin/env python3
"""ivert_output_vector.py — convert IVERT ICESat-2 .nc granule files to GIS vector formats.

Reads one or more .nc files produced by IS2Database._process_h5_to_nc() and
writes them as geolocated point vector files (GeoPackage, Shapefile, or CSV/XYZ).

Usage
-----
    python ivert_output_vector.py <nc_file_or_dir> [<nc_file_or_dir> ...] [options]

Examples
--------
    # Single file to GeoPackage (default)
    python ivert_output_vector.py granule.nc

    # All .nc files in a directory, output as Shapefile alongside inputs
    python ivert_output_vector.py ~/.ivert/icesat2/granules/ -of shp

    # Filter to bathy photons only, send to a specific directory
    python ivert_output_vector.py granule.nc --classes 40,41 -d /tmp/out/

    # Merge all inputs into one output file
    python ivert_output_vector.py granules/ --merge -o merged_bahamas.gpkg
"""

import argparse
import glob
import os
import sys

import geopandas
import netCDF4
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLASS_NAMES = {
     1: "ground",
     2: "canopy",
     3: "canopy_top",
     7: "building",
    40: "bathy_floor",
    41: "bathy_surface",
}

SUPPORTED_FORMATS = {
    "gpkg": ("GPKG",  ".gpkg"),
    "shp":  ("ESRI Shapefile", ".shp"),
    "csv":  (None,    ".csv"),
    "xyz":  (None,    ".xyz"),
}

WGS84_EPSG = 4326


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------
def nc_to_geodataframe(nc_path: str,
                       classes: list = None) -> geopandas.GeoDataFrame:
    """Read a single .nc granule file and return a GeoDataFrame.

    Parameters
    ----------
    nc_path : str
        Path to the .nc file.
    classes : list of int, optional
        If given, keep only photons whose class_code is in this list.
    """
    with netCDF4.Dataset(nc_path) as ds:
        def _arr(name):
            v = ds.variables[name][:]
            return v.data if hasattr(v, "data") else np.array(v)

        x          = _arr("x")
        y          = _arr("y")
        z          = _arr("z").astype(float)
        class_code = _arr("class_code").astype(int)
        confidence = _arr("confidence").astype(int)
        delta_time = _arr("delta_time")

        bathy_conf = None
        if "bathy_confidence" in ds.variables:
            bathy_conf = _arr("bathy_confidence").astype(float)

        # Pull granule-level metadata from global attributes
        granule_id = getattr(ds, "granule_id", os.path.splitext(os.path.basename(nc_path))[0])

    df = pd.DataFrame({
        "x":            x,
        "y":            y,
        "z":            z,
        "class_code":   class_code,
        "class_name":   pd.array([CLASS_NAMES.get(c, f"class_{c}") for c in class_code],
                                 dtype="string"),
        "confidence":   confidence,
        "delta_time":   delta_time,
        "granule_id":   granule_id,
    })
    if bathy_conf is not None:
        df["bathy_confidence"] = bathy_conf

    if classes is not None:
        df = df[df["class_code"].isin(classes)].reset_index(drop=True)

    if df.empty:
        return geopandas.GeoDataFrame(df, geometry=[], crs=WGS84_EPSG)

    geometry = geopandas.points_from_xy(df["x"], df["y"], df["z"])
    gdf = geopandas.GeoDataFrame(df, geometry=geometry, crs=WGS84_EPSG)
    return gdf


def write_vector(gdf: geopandas.GeoDataFrame, outpath: str, fmt_key: str,
                 overwrite: bool = False):
    """Write a GeoDataFrame to the requested vector format."""
    if os.path.exists(outpath):
        if not overwrite:
            print(f"  Skipping existing {os.path.basename(outpath)} (use -w to overwrite).")
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
# File discovery
# ---------------------------------------------------------------------------
def collect_nc_files(paths: list) -> list:
    """Expand a list of file/directory paths into a flat list of .nc files."""
    found = []
    for p in paths:
        if os.path.isdir(p):
            found.extend(sorted(glob.glob(os.path.join(p, "*.nc"))))
        elif os.path.isfile(p) and p.endswith(".nc"):
            found.append(p)
        else:
            # Treat as glob pattern
            matches = sorted(glob.glob(p))
            found.extend(m for m in matches if m.endswith(".nc"))
    return found


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def define_and_parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert IVERT ICESat-2 .nc granule files to GIS vector formats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "inputs", nargs="+",
        help=".nc file(s) or director(y/ies) containing .nc files.",
    )
    parser.add_argument(
        "-of", "--output_format", dest="output_format", default="gpkg",
        choices=list(SUPPORTED_FORMATS.keys()),
        help="Output format. Default: gpkg",
    )
    parser.add_argument(
        "-d", "--output_dir", dest="output_dir", default=None,
        help="Output directory. Default: same directory as each input file.",
    )
    parser.add_argument(
        "-o", "--output", dest="output", default=None,
        help="Explicit output filename (only valid with --merge or a single input).",
    )
    parser.add_argument(
        "--merge", action="store_true", default=False,
        help="Merge all input granules into a single output file.",
    )
    parser.add_argument(
        "--classes", default=None,
        help="Comma-separated class codes to include (e.g. '1,40,41'). "
             "Default: all classes.",
    )
    parser.add_argument(
        "-w", "--overwrite", action="store_true", default=False,
        help="Overwrite existing output files.",
    )
    return parser.parse_args()


def main():
    args = define_and_parse_args()

    fmt_key = args.output_format.lower().lstrip(".")
    _, ext   = SUPPORTED_FORMATS[fmt_key]

    classes = None
    if args.classes:
        classes = [int(c.strip()) for c in args.classes.split(",")]

    nc_files = collect_nc_files(args.inputs)
    if not nc_files:
        sys.exit("No .nc files found.")

    if args.output_dir and not os.path.isdir(args.output_dir):
        sys.exit(f"Output directory does not exist: {args.output_dir}")

    # ------------------------------------------------------------------
    # Merged mode: combine all granules into one output file
    # ------------------------------------------------------------------
    if args.merge:
        gdfs = []
        for nc_path in nc_files:
            print(f"Reading {os.path.basename(nc_path)} ...", flush=True)
            gdf = nc_to_geodataframe(nc_path, classes=classes)
            print(f"  {len(gdf):,} photons")
            gdfs.append(gdf)

        if not gdfs:
            sys.exit("No photons found after filtering.")

        merged = geopandas.GeoDataFrame(
            pd.concat(gdfs, ignore_index=True),
            crs=WGS84_EPSG,
        )

        if args.output:
            outpath = args.output
        else:
            outdir = args.output_dir or os.path.dirname(nc_files[0])
            outpath = os.path.join(outdir, "merged" + ext)

        write_vector(merged, outpath, fmt_key, overwrite=args.overwrite)
        return

    # ------------------------------------------------------------------
    # Per-file mode
    # ------------------------------------------------------------------
    if args.output and len(nc_files) > 1:
        sys.exit("--output requires exactly one input file (or use --merge).")

    for nc_path in nc_files:
        print(f"\n{os.path.basename(nc_path)}", flush=True)
        gdf = nc_to_geodataframe(nc_path, classes=classes)
        print(f"  {len(gdf):,} photons")

        if args.output:
            outpath = args.output
        else:
            stem = os.path.splitext(os.path.basename(nc_path))[0]
            outdir = args.output_dir or os.path.dirname(nc_path)
            outpath = os.path.join(outdir, stem + ext)

        write_vector(gdf, outpath, fmt_key, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
