"""Tests for reading a DEM band's values: its nodata value, scale/offset, and CRS."""

import logging
import types

import netCDF4
import numpy as np
import pyproj
import pytest
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
    """Packed DEMs store scaled integers; validating the raw values would compare against non-elevations."""
    path = _packed_netcdf(tmp_path / "packed.nc")

    with rasterio.open(path) as ds:
        stored = ds.read(1)
        elevs = validate_dem._unpack_elevations(stored.ravel(), ds, 1)

    assert stored.dtype == np.int16
    np.testing.assert_allclose(elevs, stored.ravel() * 0.01 - 100.0)


def test_unpacked_values_are_unchanged_without_scale_or_offset():
    """Most DEMs have no scale or offset, and their values are returned as they are, without a copy."""
    stored = np.array([1, 2, 3], dtype=np.int16)
    ds = types.SimpleNamespace(scales=(1.0,), offsets=(0.0,), name="dem.tif")

    assert validate_dem._unpack_elevations(stored, ds, 1) is stored


def test_scale_and_offset_come_from_the_band_validated():
    """-bn/--band-num 2 must use band 2's scale and offset, not band 1's."""
    stored = np.array([10], dtype=np.int16)
    ds = types.SimpleNamespace(scales=(1.0, 0.5), offsets=(0.0, 2.0), name="dem.tif")

    np.testing.assert_allclose(validate_dem._unpack_elevations(stored, ds, 2), [7.0])


def test_nodata_comes_from_the_band_validated():
    """Band 2 was once masked with band 1's nodata, which is what rasterio's dataset.nodata gives."""
    ds = types.SimpleNamespace(nodatavals=(-9999.0, -32768.0))

    assert validate_dem._band_nodata(ds, 1) == -9999.0
    assert validate_dem._band_nodata(ds, 2) == -32768.0


def test_nodata_priority():
    """--ndv wins over the band's own value, and the configured default fills in where it has none."""
    ds = types.SimpleNamespace(nodatavals=(None, -32768.0))

    assert validate_dem._band_nodata(ds, 2, user_ndv=0.0, default_ndv=-1.0) == 0.0
    assert validate_dem._band_nodata(ds, 1, default_ndv=-1.0) == -1.0


def test_split_pieces_keep_the_scale_and_offset(tmp_path):
    """Large DEMs are validated in pieces, which would be read as raw integers without these."""
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


@pytest.fixture
def no_transformez_check(monkeypatch):
    """Skip transformez's check of vertical references; these tests only test the choosing."""
    monkeypatch.setattr(
        validate_dem.ivert.vdatum_lookup,
        "check_vdatum",
        lambda _ref: None,
    )


WGS84 = pyproj.CRS("EPSG:4326")
EGM2008 = pyproj.CRS("EPSG:3855")
NAVD88 = pyproj.CRS("EPSG:5703")


@pytest.mark.usefixtures("no_transformez_check")
def test_missing_crs_without_projection_is_an_error():
    """A DEM with no CRS once failed with an AttributeError; now the message points to -p."""
    with pytest.raises(ValueError, match="-p/--projection"):
        validate_dem._resolve_dem_crs("dem.nc", None, EGM2008)


@pytest.mark.usefixtures("no_transformez_check")
def test_missing_vertical_datum_is_an_error():
    """With no vertical datum from -V, -p or the file, there is nothing to shift photons to."""
    with pytest.raises(ValueError, match="-V/--vdatum"):
        validate_dem._resolve_dem_crs("dem.tif", WGS84, None)


@pytest.mark.usefixtures("no_transformez_check")
def test_file_crs_is_used_when_nothing_is_given(caplog):
    """The common case stays quiet: no flags, the file's CRS, no warnings."""
    horz, vert = validate_dem._resolve_dem_crs("dem.tif", WGS84, EGM2008)

    assert horz.equals(WGS84)
    assert vert.equals(EGM2008)
    assert not caplog.records


@pytest.mark.usefixtures("no_transformez_check")
def test_projection_fills_in_a_missing_crs(caplog):
    """Filling a gap isn't an override, so there is no warning."""
    horz, _ = validate_dem._resolve_dem_crs("dem.nc", None, EGM2008, "EPSG:26910")

    assert horz.equals(pyproj.CRS("EPSG:26910"))
    assert not caplog.records


@pytest.mark.usefixtures("no_transformez_check")
def test_projection_overrides_the_file_crs_with_a_warning(caplog):
    """-p wins over the file's CRS, with a warning in case the difference wasn't intended."""
    with caplog.at_level(logging.WARNING):
        horz, _ = validate_dem._resolve_dem_crs("dem.tif", WGS84, EGM2008, "EPSG:26910")

    assert horz.equals(pyproj.CRS("EPSG:26910"))
    assert "in place of the CRS in the file" in caplog.text


@pytest.mark.usefixtures("no_transformez_check")
def test_matching_projection_logs_no_warning(caplog):
    """The file's own CRS, spelled another way, isn't an override."""
    with caplog.at_level(logging.WARNING):
        validate_dem._resolve_dem_crs("dem.tif", WGS84, EGM2008, "4326")

    assert not caplog.records


@pytest.mark.usefixtures("no_transformez_check")
@pytest.mark.parametrize(
    ("projection", "expected_horz", "expected_vert"),
    [
        ("EPSG:4326+3855", "EPSG:4326", 3855),
        ("EPSG:4326+5703", "EPSG:4326", 5703),
        ("EPSG:6893", "EPSG:3395", 3855),
    ],
)
def test_compound_projection_gives_the_vertical_datum(
    projection,
    expected_horz,
    expected_vert,
):
    """-p may carry the vertical datum too, as one compound code or two codes joined by '+'."""
    horz, vert = validate_dem._resolve_dem_crs("dem.nc", None, None, projection)

    assert horz.equals(pyproj.CRS(expected_horz))
    assert vert.to_epsg() == expected_vert


@pytest.mark.usefixtures("no_transformez_check")
def test_projection_with_a_transformez_tidal_datum():
    """A tidal datum has no EPSG code, so its transformez reference is kept as a string."""
    horz, vert = validate_dem._resolve_dem_crs(
        "dem.nc",
        None,
        None,
        "EPSG:4326+vdatum:mllw",
    )

    assert horz.equals(WGS84)
    assert vert == "vdatum:mllw"


@pytest.mark.usefixtures("no_transformez_check")
def test_vdatum_overrides_the_projection_vertical_with_a_warning(
    caplog,
):
    """-V outranks the vertical part of -p, and says so when they differ."""
    with caplog.at_level(logging.WARNING):
        _, vert = validate_dem._resolve_dem_crs(
            "dem.nc",
            None,
            None,
            "EPSG:4326+3855",
            "navd88",
        )

    assert vert.equals(NAVD88)
    assert "in place of the vertical datum in -p/--projection" in caplog.text


@pytest.mark.usefixtures("no_transformez_check")
def test_vdatum_matching_the_projection_vertical_logs_no_warning(
    caplog,
):
    """'egm2008' and EPSG:3855 are the same datum, so this isn't an override."""
    with caplog.at_level(logging.WARNING):
        validate_dem._resolve_dem_crs("dem.nc", None, None, "EPSG:4326+3855", "egm2008")

    assert not caplog.records


@pytest.mark.usefixtures("no_transformez_check")
@pytest.mark.parametrize(
    ("projection", "vdatum", "flag"),
    [
        (None, "navd88", "-V/--vdatum"),
        ("EPSG:4326+5703", None, "-p/--projection"),
    ],
)
def test_command_line_vertical_overrides_the_file_with_a_warning(
    caplog,
    projection,
    vdatum,
    flag,
):
    """Either flag outranks the file's vertical datum, and the warning names the flag."""
    with caplog.at_level(logging.WARNING):
        _, vert = validate_dem._resolve_dem_crs(
            "dem.tif",
            WGS84,
            EGM2008,
            projection,
            vdatum,
        )

    assert vert.equals(NAVD88)
    assert (
        f"Using {flag} EPSG:5703 for dem.tif in place of the vertical datum in the file"
        in (caplog.text)
    )


@pytest.mark.usefixtures("no_transformez_check")
def test_projection_without_a_horizontal_crs_is_an_error():
    """A vertical-only code given to -p can't place the DEM horizontally."""
    with pytest.raises(ValueError, match="no horizontal CRS"):
        validate_dem._resolve_dem_crs("dem.tif", WGS84, EGM2008, "EPSG:5703")


def test_exports_use_the_projection():
    """Error exports are written in -p's horizontal CRS, without a transformez vertical part."""
    ds = types.SimpleNamespace(crs=None)

    assert validate_dem._output_crs(ds, None) is None
    assert validate_dem._output_crs(ds, "EPSG:26910").to_epsg() == 26910
    assert validate_dem._output_crs(ds, "EPSG:4326+vdatum:mllw").to_epsg() == 4326
