"""Tests for the bathymetry filter settings."""

import pytest

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
