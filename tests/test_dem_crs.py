"""Tests for reading a DEM's CRS from its file."""

import numpy as np
import pyproj
import rasterio
import rasterio.transform
from rasterio.shutil import copy as copy_raster

from ivert.utils import dem_geom


def _write_tif(path, crs="EPSG:4326"):
    """Write a 4x5 GeoTIFF at 105-104.5 W, 39.9-40.3 N (or in 'crs' units), or with no CRS."""
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=5,
        height=4,
        count=1,
        dtype="float32",
        crs=crs,
        transform=rasterio.transform.from_origin(-105.0, 40.3, 0.1, 0.1),
    ) as dst:
        dst.write(np.zeros((1, 4, 5), dtype="f4"))
    return str(path)


def test_ogc_crs84_reads_as_epsg_4326():
    horz = dem_geom.get_dem_reference_frame_from_user_input("OGC:CRS84", "horz")

    assert horz.equals(pyproj.CRS("EPSG:4326"))
    assert dem_geom.get_dem_srs_string(horz, pyproj.CRS(3855)) == "EPSG:4326+3855"


def test_ascii_grid_crs_pairs_with_an_epsg_vertical_datum(tmp_path):
    """GDAL reports ASCII Grid's geographic CRS as OGC:CRS84."""
    asc = str(tmp_path / "dem.asc")
    copy_raster(_write_tif(tmp_path / "dem.tif"), asc, driver="AAIGrid")

    horz = dem_geom.get_dem_reference_frame_from_file(asc, "horz")

    assert dem_geom.get_dem_srs_string(horz, pyproj.CRS(5703)) == "EPSG:4326+5703"


def test_split_srs_string_reads_ogc_crs84_as_epsg_4326():
    horz, vert = dem_geom.split_srs_string("OGC:CRS84+vdatum:mllw")

    assert horz.equals(pyproj.CRS("EPSG:4326"))
    assert vert == "vdatum:mllw"
