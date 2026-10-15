"""Tests for a DEM file's CRS and the DEM region 'ivert database download' uses."""

import logging

import h5py
import netCDF4
import numpy as np
import pyproj
import pytest
import rasterio
import rasterio.transform
import shapely
from rasterio.shutil import copy as copy_raster

from ivert import validate_dem
from ivert.cli import _region_from_file
from ivert.utils import dem_geom
from ivert.utils.dem_source import DEMVariableError


def _write_tif(path, crs="EPSG:4326"):
    """Write a 4x5 GeoTIFF at 105-104.5 W, 39.9-40.3 N, or with no CRS.

    Coordinates are in 'crs' units when it is given.
    """
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
    """OGC:CRS84 with an EPSG vertical datum failed: their authorities must match."""
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
    """The same reading applies to an SRS string, even one with a transformez datum."""
    horz, vert = dem_geom.split_srs_string("OGC:CRS84+vdatum:mllw")

    assert horz.equals(pyproj.CRS("EPSG:4326"))
    assert vert == "vdatum:mllw"


def test_download_region_from_a_dem(tmp_path):
    """'ivert database download' given a DEM fetches photons for the DEM's extent."""
    bbox, geometry = _region_from_file(_write_tif(tmp_path / "dem.tif"))

    np.testing.assert_allclose(bbox, (-105.0, -104.5, 39.9, 40.3))
    assert geometry is None


def test_download_region_from_a_dem_without_a_crs_needs_projection(tmp_path):
    """A DEM with no CRS once raised AttributeError; now the message points to -p."""
    path = _write_tif(tmp_path / "dem.tif", crs=None)

    with pytest.raises(ValueError, match="-p/--projection"):
        _region_from_file(path)
    bbox, _ = _region_from_file(path, projection_horz=pyproj.CRS("EPSG:4326"))
    np.testing.assert_allclose(bbox, (-105.0, -104.5, 39.9, 40.3))


def test_download_projection_overrides_the_dem_crs_with_a_warning(tmp_path, caplog):
    """-p wins over the file's CRS, with a warning in case that wasn't intended."""
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


# ---------------------------------------------------------------------------
# A CRS from the user, split into its horizontal and vertical parts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("crs", "horz", "vert"),
    [
        ("EPSG:4326+3855", "EPSG:4326", "EPSG:3855"),  # compound
        ("EPSG:4979", "EPSG:4979", "EPSG:4979"),  # 3D: its own vertical reference
        ("EPSG:5703", None, "EPSG:5703"),  # vertical only
        ("EPSG:26910", "EPSG:26910", None),  # horizontal only
        ("OGC:CRS84", "EPSG:4326", None),  # given as its EPSG equivalent
        (None, None, None),
        ("", None, None),
    ],
)
def test_user_input_crs_splits_into_horizontal_and_vertical(crs, horz, vert):
    """Each kind of CRS comes apart into the (horizontal, vertical) pair it holds."""
    got_horz, got_vert = dem_geom.get_dem_reference_frame_from_user_input(crs)

    assert (None if got_horz is None else got_horz.to_string()) == horz
    assert (None if got_vert is None else got_vert.to_string()) == vert


def test_user_input_crs_returns_the_part_asked_for():
    """'h' and 'v' (or any word starting with them) pick one part."""
    crs = "EPSG:4326+3855"

    assert dem_geom.get_dem_reference_frame_from_user_input(crs, "horz") == (
        pyproj.CRS("EPSG:4326")
    )
    assert dem_geom.get_dem_reference_frame_from_user_input(crs, "v") == (
        pyproj.CRS("EPSG:3855")
    )
    with pytest.raises(ValueError, match="Must begin with 'h', 'v', or 'b'"):
        dem_geom.get_dem_reference_frame_from_user_input(crs, "x")


# ---------------------------------------------------------------------------
# get_wgs84_bounding_box
# ---------------------------------------------------------------------------
def test_wgs84_bounding_box_of_a_projected_bbox():
    """An (xmin, xmax, ymin, ymax) bbox in UTM comes back in degrees, same order."""
    bbox = dem_geom.get_wgs84_bounding_box(
        (500000, 510000, 4000000, 4010000),
        "EPSG:26910",
    )

    assert bbox == pytest.approx((-123.0, -122.8887, 36.1447, 36.2349), abs=1e-4)


def test_wgs84_bounding_box_of_a_polygon_already_in_wgs84():
    """No transform is needed, so the polygon's own bounds come back."""
    polygon = shapely.geometry.box(-105.0, 39.9, -104.5, 40.3)

    assert dem_geom.get_wgs84_bounding_box(polygon, "EPSG:4326") == pytest.approx(
        (-105.0, -104.5, 39.9, 40.3),
    )


def test_wgs84_bounding_box_of_a_coordinate_list():
    """An even-length list longer than 4 values is taken as polygon vertices."""
    ring = [(-105.0, 39.9), (-104.5, 39.9), (-104.5, 40.3), (-105.0, 40.3)]

    assert dem_geom.get_wgs84_bounding_box(ring + ring[:2], "EPSG:4326") == (
        pytest.approx((-105.0, -104.5, 39.9, 40.3))
    )


def test_wgs84_bounding_box_of_a_dem_file(tmp_path):
    """A DEM file's CRS comes from the file when none is given."""
    path = _write_tif(tmp_path / "dem.tif")

    assert dem_geom.get_wgs84_bounding_box(path) == pytest.approx(
        (-105.0, -104.5, 39.9, 40.3),
    )


def test_wgs84_bounding_box_of_a_missing_file_is_an_error(tmp_path):
    """A string that isn't an existing file can't be anything else."""
    with pytest.raises(FileNotFoundError):
        dem_geom.get_wgs84_bounding_box(str(tmp_path / "missing.tif"))


def test_wgs84_bounding_box_of_a_bbox_needs_a_crs():
    """A bare bbox carries no CRS of its own."""
    with pytest.raises(ValueError, match="could not be resolved"):
        dem_geom.get_wgs84_bounding_box((-105.0, -104.5, 39.9, 40.3))


@pytest.mark.parametrize("value", [42, (1.0, 2.0, 3.0), (1.0, 2.0, 3.0, 4.0, 5.0)])
def test_wgs84_bounding_box_rejects_what_it_cannot_read(value):
    """Not a file, polygon, 4-value bbox or even-length coordinate list."""
    with pytest.raises(TypeError):
        dem_geom.get_wgs84_bounding_box(value, "EPSG:4326")


# ---------------------------------------------------------------------------
# The vertical datum the user gave, from -V/--vdatum or -p/--projection
# ---------------------------------------------------------------------------
def test_no_user_vertical_datum():
    """With neither option, the file's vertical datum will be used."""
    assert validate_dem._user_vertical_reference("dem.tif", None, None) == (None, "")


def test_user_vertical_datum_from_the_projection():
    """The vertical part of a compound -p/--projection is used when -V isn't given."""
    assert validate_dem._user_vertical_reference("dem.tif", "EPSG:3855", None) == (
        "EPSG:3855",
        "-p/--projection",
    )


def test_vdatum_overrides_the_projection_with_a_warning(caplog):
    """-V/--vdatum wins, and the user is told what it replaced."""
    with caplog.at_level(logging.WARNING):
        got = validate_dem._user_vertical_reference("dem.tif", "EPSG:5703", "EPSG:3855")

    assert got == ("EPSG:3855", "-V/--vdatum")
    assert "in place of the vertical datum in -p/--projection" in caplog.text


def test_vdatum_matching_the_projection_gives_no_warning(caplog):
    """Saying the same thing twice isn't worth a warning."""
    with caplog.at_level(logging.WARNING):
        got = validate_dem._user_vertical_reference("dem.tif", "EPSG:3855", "EPSG:3855")

    assert got == ("EPSG:3855", "-V/--vdatum")
    assert caplog.text == ""


def test_unrecognised_vdatum_is_an_error():
    """A -V/--vdatum that names no vertical datum stops the run."""
    with pytest.raises(ValueError, match="Unrecognised vertical datum"):
        validate_dem._user_vertical_reference("dem.tif", None, "not-a-datum")
