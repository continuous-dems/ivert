"""Tests for where 'ivert database convert' writes its files, given -o/--output."""

import logging
import os

import geopandas
import pytest
import shapely

from ivert import cli
from ivert import export_vector as ev


@pytest.fixture
def in_tmp(tmp_path, monkeypatch):
    """Run in tmp_path, so relative -o values and the default land there."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("output", [None, ""])
def test_without_output_the_default_name_is_used_here(in_tmp, output):
    """No -o means the command's default name in the current folder."""
    assert cli._convert_output_base(output, "ivert_photons") == in_tmp / "ivert_photons"


@pytest.mark.parametrize("output", ["mydir/", f"mydir{os.sep}"])
def test_a_trailing_separator_names_a_folder_that_is_created(in_tmp, caplog, output):
    """'-o mydir/' wrote the hidden file 'mydir/.gpkg', or 'mydir.gpkg' next to it."""
    base = cli._convert_output_base(output, "ivert_photons")

    assert base.absolute() == in_tmp / "mydir" / "ivert_photons"
    assert (in_tmp / "mydir").is_dir()
    assert "does not exist; creating it" in caplog.text
    assert (
        ev.output_path_for_format(base, "gpkg").absolute()
        == in_tmp / "mydir" / "ivert_photons.gpkg"
    )


@pytest.mark.parametrize("output", ["mydir", "."])
def test_an_existing_folder_gets_the_default_name_inside(in_tmp, output):
    """A folder named without the trailing separator is still a folder."""
    (in_tmp / "mydir").mkdir()

    base = cli._convert_output_base(output, "ATL03_granule")

    assert base.absolute() == in_tmp / output / "ATL03_granule"


def test_any_other_output_is_a_base_name(in_tmp):
    """A name that isn't a folder is used as given, and nothing is created for it."""
    base = cli._convert_output_base("sub/photons", "ivert_photons")

    assert base.absolute() == in_tmp / "sub" / "photons"
    assert not (in_tmp / "sub").exists()


@pytest.mark.parametrize("fmt", ["gpkg", "csv"])
def test_a_missing_folder_is_created_with_a_warning(tmp_path, caplog, fmt):
    """'-o out/photons' crashed in pyogrio ("unable to open database file")."""
    gdf = geopandas.GeoDataFrame({"z": [1.0]}, geometry=[shapely.Point(0, 0)])
    outpath = tmp_path / "out" / "sub" / f"photons.{fmt}"

    with caplog.at_level(logging.WARNING):
        ev.write_vector(gdf, outpath, fmt)

    assert outpath.is_file()
    assert f"{outpath.parent} does not exist; creating it" in caplog.text


def test_an_existing_folder_gives_no_warning(tmp_path, caplog):
    """The warning is only for a folder that had to be created."""
    gdf = geopandas.GeoDataFrame({"z": [1.0]}, geometry=[shapely.Point(0, 0)])

    with caplog.at_level(logging.WARNING):
        ev.write_vector(gdf, tmp_path / "photons.gpkg", "gpkg")

    assert "does not exist" not in caplog.text


@pytest.mark.parametrize(
    ("out_base", "fmt", "expected"),
    [
        ("photons", "gpkg", "photons.gpkg"),
        ("photons.gpkg", "shp", "photons.shp"),
        ("photons.GPKG", "gpkg", "photons.GPKG"),
        ("photons.Shp", "shp", "photons.Shp"),
        ("photons.GPKG", "shp", "photons.shp"),
    ],
)
def test_the_extension_is_set_per_format_keeping_the_users_spelling(
    out_base,
    fmt,
    expected,
):
    """A given extension that is already right for the format is kept as typed."""
    assert ev.output_path_for_format(out_base, fmt).name == expected
