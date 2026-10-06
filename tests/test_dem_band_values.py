"""Tests for reading a DEM band's values: its nodata value."""

import types

from ivert import validate_dem


def test_nodata_comes_from_the_band_validated():
    ds = types.SimpleNamespace(nodatavals=(-9999.0, -32768.0))

    assert validate_dem._band_nodata(ds, 1) == -9999.0
    assert validate_dem._band_nodata(ds, 2) == -32768.0


def test_nodata_priority():
    ds = types.SimpleNamespace(nodatavals=(None, -32768.0))

    assert validate_dem._band_nodata(ds, 2, user_ndv=0.0, default_ndv=-1.0) == 0.0
    assert validate_dem._band_nodata(ds, 1, default_ndv=-1.0) == -1.0
