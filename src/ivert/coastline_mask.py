#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""coastline_mask.py -- Generate a DEM-aligned land/water (coastline) mask.

Used by IVERT validation to classify each DEM cell as onshore or offshore so that
mis-classified ICESat-2 photons (e.g. false 'bathy_floor' offshore, false 'ground'
offshore) can be filtered out of validation results.

The mask is produced by globato's GlobCoast module (part of the continuous-dems
package), which synthesizes a land=1 / water=0 raster from one or more configurable
sources (e.g. OpenStreetMap coastlines, the Copernicus DEM) using weighted voting.
The synthesized raster is then resampled onto the input DEM's exact grid, so each
results cell (i, j) indexes directly into the mask array.

This module deliberately does NOT use the deprecated 'cudem' library.
"""

import argparse
import logging
import os
import sys

import numpy
from osgeo import gdal, osr

import fetchez.spatial
from fetchez.registry import ModuleRegistry
from globato.modules.glob_coast import GlobCoast

import utils.configfile
import utils.dem_geom as dem_geom

ivert_config = utils.configfile.Config()
logger = logging.getLogger(__name__)

# Mask pixel values.
MASK_WATER = 0    # offshore
MASK_LAND = 1     # onshore
MASK_NODATA = 255  # unknown (treated as "keep" by the misclassification filter)


def _dem_grid_and_bbox(dem_name: str):
    """Return (projection_wkt, geotransform, xsize, ysize, native_bbox, wgs84_bbox).

    native_bbox is (xmin, ymin, xmax, ymax) in the DEM's own CRS.
    wgs84_bbox is (xmin, xmax, ymin, ymax) in EPSG:4326.
    """
    dem_ds = gdal.Open(dem_name, gdal.GA_ReadOnly)
    if dem_ds is None:
        raise RuntimeError(f"Could not open DEM '{dem_name}'.")

    gt = dem_ds.GetGeoTransform()
    projection = dem_ds.GetProjection()
    xsize, ysize = dem_ds.RasterXSize, dem_ds.RasterYSize

    xmin = gt[0]
    xmax = gt[0] + (gt[1] * xsize)
    ymax = gt[3]
    ymin = gt[3] + (gt[5] * ysize)  # gt[5] is negative for north-up rasters
    native_bbox = (xmin, ymin, xmax, ymax)

    # (xmin, xmax, ymin, ymax) in WGS84
    wgs84_bbox = dem_geom.get_wgs84_bounding_box(dem_name)

    dem_ds = None
    return projection, gt, xsize, ysize, native_bbox, wgs84_bbox


def get_or_create_coastline_mask(dem_name: str,
                                 output_fname: str | None = None,
                                 sources: str | None = None,
                                 overwrite: bool = False,
                                 verbose: bool = True) -> str | None:
    """Generate (or reuse) a coastline mask raster aligned to the input DEM's grid.

    The output is a single-band uint8 GeoTIFF on the DEM's exact grid and CRS, with
    land=1 (MASK_LAND), water=0 (MASK_WATER), and MASK_NODATA where unknown.

    Args:
        dem_name: Path to the input DEM.
        output_fname: Output mask path. Defaults to '<dem-base>_coastline_mask.tif'.
        sources: Comma-separated globato/fetchez source list (e.g. 'osm_landmask,copernicus').
            Defaults to the 'coastline_mask_sources' config value.
        overwrite: If False and the output already exists, reuse it.
        verbose: Print progress messages.

    Returns:
        The path to the mask raster, or None if it could not be generated. Callers
        should treat None as "skip coastline-based filtering" rather than an error.
    """
    if output_fname is None:
        base, _ = os.path.splitext(dem_name)
        output_fname = base + "_coastline_mask.tif"

    if os.path.exists(output_fname) and not overwrite:
        if verbose:
            print("Using existing coastline mask", os.path.basename(output_fname) + ".")
        return output_fname

    if sources is None:
        sources = ivert_config.coastline_mask_sources

    try:
        projection, _, xsize, ysize, native_bbox, wgs84_bbox = _dem_grid_and_bbox(dem_name)
        wgs_xmin, wgs_xmax, wgs_ymin, wgs_ymax = wgs84_bbox

        # GlobCoast works in EPSG:4326. Build its region (west/east/south/north) and a
        # target resolution at least as fine as the DEM (in degrees), then resample to
        # the DEM grid afterwards.
        region = fetchez.spatial.parse_region(
            f"{wgs_xmin}/{wgs_xmax}/{wgs_ymin}/{wgs_ymax}")[0]
        res_deg = min((wgs_xmax - wgs_xmin) / max(xsize, 1),
                      (wgs_ymax - wgs_ymin) / max(ysize, 1))

        cache_dir = os.path.join(ivert_config.cache_directory, "coastline_mask")
        os.makedirs(cache_dir, exist_ok=True)

        if verbose:
            print(f"Generating coastline mask from [{sources}] for "
                  f"{os.path.basename(dem_name)} ...")

        # GlobCoast looks its source modules up by name via the fetchez ModuleRegistry, which
        # the fetchez/globato CLIs populate at startup. We're calling it programmatically, so we
        # must load the registry ourselves or every source resolves to "Unknown source".
        ModuleRegistry.load_all()

        mod = GlobCoast(res=str(res_deg),
                        sources=sources,
                        src_region=region,
                        outdir=cache_dir)
        mod.run()

        globcoast_fn = mod.out_fn
        if not globcoast_fn or not os.path.exists(globcoast_fn):
            logger.warning("GlobCoast produced no output raster for %s.", dem_name)
            if verbose:
                print("Warning: coastline mask generation produced no output.")
            return None

        # Resample/reproject GlobCoast's EPSG:4326 land/water raster onto the DEM grid.
        # Strip any vertical component from a compound CRS — GDAL would otherwise treat
        # the binary 0/1 mask pixel values as elevations and apply a vertical datum shift.
        warp_srs = projection if projection else "EPSG:4326"
        if projection:
            _srs = osr.SpatialReference(wkt=projection)
            if _srs.IsCompound():
                _srs.StripVertical()
                warp_srs = _srs.ExportToWkt()

        xmin, ymin, xmax, ymax = native_bbox
        warp_opts = gdal.WarpOptions(
            format="GTiff",
            dstSRS=warp_srs,
            outputBounds=(xmin, ymin, xmax, ymax),
            width=xsize,
            height=ysize,
            resampleAlg="near",
            outputType=gdal.GDT_Byte,
            dstNodata=MASK_NODATA,
            creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])

        out_ds = gdal.Warp(output_fname, globcoast_fn, options=warp_opts)
        if out_ds is None:
            logger.warning("gdal.Warp failed to align coastline mask for %s.", dem_name)
            return None
        out_ds = None

        if verbose:
            print(os.path.basename(output_fname), "written.")
        return output_fname

    except Exception as e:  # noqa: BLE001 -- mask generation must never break validation
        logger.warning("Could not generate coastline mask for %s: %s", dem_name, e)
        if verbose:
            print(f"Warning: could not generate coastline mask for "
                  f"{os.path.basename(dem_name)}: {e}")
        return None


def load_coastline_mask_array(mask_fname: str) -> numpy.ndarray:
    """Read a coastline mask raster (band 1) as a numpy array aligned to the DEM grid.

    Values are MASK_LAND (1), MASK_WATER (0), or MASK_NODATA (255).
    """
    ds = gdal.Open(mask_fname, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Could not open coastline mask '{mask_fname}'.")
    array = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return array


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a DEM-aligned land/water (coastline) mask raster.")
    parser.add_argument("input_dem", type=str, help="Input DEM file.")
    parser.add_argument("output_fname", type=str, nargs="?", default=None,
                        help="Output mask path. Default: '<dem-base>_coastline_mask.tif'.")
    parser.add_argument("--sources", type=str, default=None,
                        help="Comma-separated source list (default: config "
                             "'coastline_mask_sources').")
    parser.add_argument("--overwrite", action="store_true", default=False,
                        help="Regenerate the mask even if it already exists.")
    parser.add_argument("--quiet", action="store_true", default=False,
                        help="Suppress progress messages.")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    result = get_or_create_coastline_mask(args.input_dem,
                                          output_fname=args.output_fname,
                                          sources=args.sources,
                                          overwrite=args.overwrite,
                                          verbose=not args.quiet)
    if result is None:
        sys.exit(1)
