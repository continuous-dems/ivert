"""Tests for the bathymetry filter settings, and for reading the reference grid."""

import numpy as np
import pytest
import rasterio.transform

from ivert import bathy_filters


def test_ref_raster_expands_home(tmp_path, monkeypatch):
    """A quoted '~' from --bathy-ref-raster reaches the settings unexpanded.

    It was once read as a folder named '~'.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = bathy_filters.BathyFilterSettings(ref_raster="~/ref.tif")

    assert settings.ref_raster == str(tmp_path / "ref.tif")


def test_ref_raster_unknown_user_is_a_value_error():
    """A '~user' with no such user must give a ValueError.

    'ivert validate' shows it as a usage error.
    """
    with pytest.raises(ValueError, match="no_such_ivert_user"):
        bathy_filters.BathyFilterSettings(ref_raster="~no_such_ivert_user/ref.tif")


def test_reference_values_are_sampled_at_points():
    """Each point reads the cell it falls in; a point off the grid reads NaN.

    rasterio 1.4.1 gives row and column numbers as floats, which can't index an
    array, so this raised IndexError and stopped any validation with the 'deep' or
    'reference' rule on.
    """
    grid = np.arange(12, dtype=float).reshape(3, 4)
    transform = rasterio.transform.from_origin(0.0, 3.0, 1.0, 1.0)

    values = bathy_filters._sample(
        grid,
        transform,
        np.array([0.5, 3.5, 1.5, 9.0]),
        np.array([2.5, 0.5, 1.5, 1.5]),
    )

    np.testing.assert_array_equal(values, [0.0, 11.0, 5.0, np.nan])
