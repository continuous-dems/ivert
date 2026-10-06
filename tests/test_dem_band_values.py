"""Tests for reading a DEM band's values: its nodata value and scale/offset."""

import types

import netCDF4
import numpy as np
import rasterio
import rasterio.transform

from ivert import validate_dem
from ivert.utils import split_dem


def _packed_netcdf(path, scale=0.01, offset=-100.0):
    """Write a lat/lon grid of int16 values packed with scale_factor/add_offset."""
    with netCDF4.Dataset(path, "w") as ds:
        ds.createDimension("lat", 4)
        ds.createDimension("lon", 5)
        lat = ds.createVariable("lat", "f8", ("lat",))
        lat[:] = np.linspace(40.0, 40.3, 4)
        lat.units = "degrees_north"
        lon = ds.createVariable("lon", "f8", ("lon",))
        lon[:] = np.linspace(-105.0, -104.6, 5)
        lon.units = "degrees_east"
        elev = ds.createVariable("elev", "i2", ("lat", "lon"), fill_value=-32768)
        elev.scale_factor = scale
        elev.add_offset = offset
        elev.set_auto_maskandscale(False)
        elev[:] = np.arange(20, dtype="i2").reshape(4, 5) * 100
    return str(path)


def test_packed_netcdf_values_are_unpacked(tmp_path):
    path = _packed_netcdf(tmp_path / "packed.nc")

    with rasterio.open(path) as ds:
        stored = ds.read(1)
        elevs = validate_dem._unpack_elevations(stored.ravel(), ds, 1)

    assert stored.dtype == np.int16
    np.testing.assert_allclose(elevs, stored.ravel() * 0.01 - 100.0)


def test_unpacked_values_are_unchanged_without_scale_or_offset():
    stored = np.array([1, 2, 3], dtype=np.int16)
    ds = types.SimpleNamespace(scales=(1.0,), offsets=(0.0,), name="dem.tif")

    assert validate_dem._unpack_elevations(stored, ds, 1) is stored


def test_scale_and_offset_come_from_the_band_validated():
    stored = np.array([10], dtype=np.int16)
    ds = types.SimpleNamespace(scales=(1.0, 0.5), offsets=(0.0, 2.0), name="dem.tif")

    np.testing.assert_allclose(validate_dem._unpack_elevations(stored, ds, 2), [7.0])


def test_nodata_comes_from_the_band_validated():
    ds = types.SimpleNamespace(nodatavals=(-9999.0, -32768.0))

    assert validate_dem._band_nodata(ds, 1) == -9999.0
    assert validate_dem._band_nodata(ds, 2) == -32768.0


def test_nodata_priority():
    ds = types.SimpleNamespace(nodatavals=(None, -32768.0))

    assert validate_dem._band_nodata(ds, 2, user_ndv=0.0, default_ndv=-1.0) == 0.0
    assert validate_dem._band_nodata(ds, 1, default_ndv=-1.0) == -1.0


def test_split_pieces_keep_the_scale_and_offset(tmp_path):
    src = tmp_path / "packed.tif"
    with rasterio.open(
        src,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="int16",
        crs="EPSG:4326",
        transform=rasterio.transform.from_origin(-105.0, 40.0, 0.1, 0.1),
    ) as dst:
        dst.write(np.arange(16, dtype="int16").reshape(1, 4, 4))
        dst.scales = (0.01,)
        dst.offsets = (-100.0,)

    pieces = split_dem.split([str(src)], factor=2, output_dir=str(tmp_path))

    assert len(pieces) == 4
    with rasterio.open(pieces[0]) as piece:
        assert piece.scales == (0.01,)
        assert piece.offsets == (-100.0,)
