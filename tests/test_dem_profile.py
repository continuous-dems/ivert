"""A DEM's profile along a laser track, moved to the photons' vertical datum."""

import logging

import numpy as np
import pyproj
import pytest

import ivert.transform_points
from ivert.plot_photon_clouds_v2 import _dem_profile_to_vert
from ivert.utils import dem_geom

LONS = np.array([-105.0, -104.9, -104.8])
LATS = np.array([40.0, 40.1, 40.2])
# The middle sample is off the DEM.
Z_DEM = np.array([10.0, np.nan, 30.0])


@pytest.fixture
def dem_vert(monkeypatch):
    """Set the vertical datum the DEM file is read as having (an EPSG code or None)."""

    def set_vert(epsg):
        vert = None if epsg is None else pyproj.CRS.from_epsg(epsg)
        monkeypatch.setattr(
            dem_geom,
            "get_dem_reference_frame_from_file",
            lambda _path: (pyproj.CRS("EPSG:4326"), vert),
        )

    return set_vert


@pytest.fixture
def transform_calls(monkeypatch):
    """Replace transform_points with one that adds 100 m, recording each call."""
    calls = []

    def fake_transform(x, y, z, *, src_epsg, dst_epsg, **_kwargs: object):
        calls.append((list(x), list(y), list(z), src_epsg, dst_epsg))
        return x, y, z + 100.0

    monkeypatch.setattr(ivert.transform_points, "transform_points", fake_transform)
    return calls


def _profile(target_vert="3855"):
    return _dem_profile_to_vert(
        "dem.tif",
        LONS,
        LATS,
        Z_DEM.copy(),
        target_vert=target_vert,
    )


def test_the_profile_is_transformed_and_its_gaps_kept(dem_vert, transform_calls):
    """Only the finite samples are transformed; the NaN stays where it was."""
    dem_vert(5703)

    z = _profile()

    np.testing.assert_array_equal(z, [110.0, np.nan, 130.0])
    assert transform_calls == [
        (
            [-105.0, -104.8],
            [40.0, 40.2],
            [10.0, 30.0],
            "EPSG:4326+5703",
            "EPSG:4326+3855",
        ),
    ]


def test_a_profile_already_in_the_target_datum_is_left_alone(
    dem_vert,
    transform_calls,
):
    """No transform when the DEM is in the datum the photons are."""
    dem_vert(3855)

    np.testing.assert_array_equal(_profile(), Z_DEM)
    assert transform_calls == []


def test_a_dem_without_a_vertical_datum_is_left_alone(dem_vert, transform_calls):
    """With nothing to transform from, the profile is plotted as it is."""
    dem_vert(None)

    np.testing.assert_array_equal(_profile(), Z_DEM)
    assert transform_calls == []


def test_an_unreadable_dem_crs_leaves_the_profile_alone(monkeypatch, transform_calls):
    """Failing to read the DEM's CRS isn't worth losing the plot over."""

    def unreadable(_path):
        msg = "no CRS"
        raise ValueError(msg)

    monkeypatch.setattr(dem_geom, "get_dem_reference_frame_from_file", unreadable)

    np.testing.assert_array_equal(_profile(), Z_DEM)
    assert transform_calls == []


def test_a_failed_transform_leaves_the_profile_alone(dem_vert, monkeypatch, caplog):
    """A transform failure is logged, and the untransformed profile still plotted."""
    dem_vert(5703)

    def failing_transform(*_args: object, **_kwargs: object):
        msg = "no grid"
        raise RuntimeError(msg)

    monkeypatch.setattr(ivert.transform_points, "transform_points", failing_transform)

    with caplog.at_level(logging.WARNING):
        z = _profile()

    np.testing.assert_array_equal(z, Z_DEM)
    assert "DEM vertical transform failed: no grid" in caplog.text
