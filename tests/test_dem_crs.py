"""Tests for reading a DEM's CRS from its file, and for the DEM region 'ivert database download' uses."""

import logging

import h5py
import netCDF4
import numpy as np
import pyproj
import pytest
import rasterio
import rasterio.transform
from rasterio.shutil import copy as copy_raster

from ivert.cli import _region_from_file
from ivert.utils import dem_geom
from ivert.utils.dem_source import DEMVariableError


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
    """OGC:CRS84 with an EPSG vertical datum failed: the datums' authorities must match."""
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
    """The same reading applies to an SRS string, including one with a transformez datum."""
    horz, vert = dem_geom.split_srs_string("OGC:CRS84+vdatum:mllw")

    assert horz.equals(pyproj.CRS("EPSG:4326"))
    assert vert == "vdatum:mllw"


def test_download_region_from_a_dem(tmp_path):
    """'ivert database download' given a DEM file fetches photons for the DEM's extent."""
    bbox, geometry = _region_from_file(_write_tif(tmp_path / "dem.tif"))

    np.testing.assert_allclose(bbox, (-105.0, -104.5, 39.9, 40.3))
    assert geometry is None


def test_download_region_from_a_dem_without_a_crs_needs_projection(tmp_path):
    """A DEM with no CRS once failed with an AttributeError; now the message points to -p."""
    path = _write_tif(tmp_path / "dem.tif", crs=None)

    with pytest.raises(ValueError, match="-p/--projection"):
        _region_from_file(path)
    bbox, _ = _region_from_file(path, projection_horz=pyproj.CRS("EPSG:4326"))
    np.testing.assert_allclose(bbox, (-105.0, -104.5, 39.9, 40.3))


def test_download_projection_overrides_the_dem_crs_with_a_warning(tmp_path, caplog):
    """-p wins over the file's CRS, with a warning in case the difference wasn't intended."""
    path = _write_tif(tmp_path / "dem.tif", crs="EPSG:4269")

    with caplog.at_level(logging.WARNING):
        _region_from_file(path, projection_horz=pyproj.CRS("EPSG:4326"))

    assert "in place of the CRS in the file" in caplog.text


def test_download_region_uses_the_variable(tmp_path):
    """A multi-variable file has no extent of its own; the chosen variable's is used."""
    path = tmp_path / "dem.nc"
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("lat", 4)
        ds.createDimension("lon", 5)
        lat = ds.createVariable("lat", "f8", ("lat",))
        lat[:] = [40.0, 40.1, 40.2, 40.3]
        lat.units = "degrees_north"
        lon = ds.createVariable("lon", "f8", ("lon",))
        lon[:] = [-105.0, -104.9, -104.8, -104.7, -104.6]
        lon.units = "degrees_east"
        for name in ("depth", "uncert"):
            ds.createVariable(name, "f4", ("lat", "lon"))[:] = np.zeros((4, 5))

    with pytest.raises(DEMVariableError):
        _region_from_file(str(path))
    bbox, _ = _region_from_file(
        str(path),
        variable="depth",
        projection_horz=pyproj.CRS("EPSG:4326"),
    )
    np.testing.assert_allclose(bbox, (-105.05, -104.55, 39.95, 40.35))


def test_download_region_from_an_ungeoreferenced_dem_is_an_error(tmp_path):
    """Plain HDF5 has no georeferencing GDAL can read."""
    path = tmp_path / "dem.h5"
    with h5py.File(path, "w") as f:
        f["elev"] = np.zeros((4, 5), dtype="f4")

    with pytest.raises(ValueError, match="no georeferencing"):
        _region_from_file(str(path), projection_horz=pyproj.CRS("EPSG:4326"))
