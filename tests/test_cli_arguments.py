"""How the CLI reads its arguments: export regions and 'ivert options set' pairs."""

import configparser
import os
from pathlib import Path

import click
import pytest

from ivert import cli
from ivert.utils.configfile import Config


# ---------------------------------------------------------------------------
# A bounding box given as 4 values
# ---------------------------------------------------------------------------
def test_bbox_values_are_west_east_south_north_by_default():
    """W/E/S/N comes back as (xmin, xmax, ymin, ymax), unchanged."""
    bbox = cli._bbox_from_values(["-120", "-119", "35", "36"], "EPSG:4326", wsen=False)

    assert bbox == (-120.0, -119.0, 35.0, 36.0)


def test_bbox_values_in_wsen_order():
    """--wsen reads W/S/E/N and still returns (xmin, xmax, ymin, ymax)."""
    assert cli._bbox_from_values(["-120", "35", "-119", "36"], "4326", wsen=True) == (
        -120.0,
        -119.0,
        35.0,
        36.0,
    )


def test_bbox_values_in_another_projection_are_converted_to_wgs84():
    """A UTM bbox comes back in degrees."""
    bbox = cli._bbox_from_values(
        ["500000", "510000", "4000000", "4010000"],
        "EPSG:26910",
        wsen=False,
    )

    assert bbox == pytest.approx((-123.0, -122.8887, 36.1447, 36.2349), abs=1e-4)


@pytest.mark.parametrize(
    "values",
    [["a", "b", "c", "d"], ["1", "2", "3"], ["1", "2", "3", "4", "5"], []],
)
def test_values_that_are_not_four_numbers_are_not_a_bbox(values):
    """None tells the caller to try the argument as a file instead."""
    assert cli._bbox_from_values(values, "EPSG:4326", wsen=False) is None


# ---------------------------------------------------------------------------
# The region argument of 'ivert database convert' and 'dump'
# ---------------------------------------------------------------------------
def test_no_region_exports_the_whole_database():
    """With no argument, everything is exported."""
    assert cli._resolve_export_target([], "EPSG:4326", wsen=False).kind == "all"


@pytest.mark.parametrize("tokens", [["-120/-119/35/36"], ["-120", "-119", "35", "36"]])
def test_four_numbers_are_a_region(tokens):
    """Slash-separated or as separate arguments, 4 numbers are a bounding box."""
    target = cli._resolve_export_target(tokens, "EPSG:4326", wsen=False)

    assert target.kind == "region"
    assert target.bbox == (-120.0, -119.0, 35.0, 36.0)
    assert target.geometry is None


def test_a_region_is_a_bbox_or_one_file():
    """Two things that aren't 4 numbers can't be a region."""
    with pytest.raises(click.ClickException, match="4-value bounding box or a single"):
        cli._resolve_export_target(["a.tif", "b.tif"], "EPSG:4326", wsen=False)


def test_a_region_file_must_exist(tmp_path):
    """A single argument that isn't a bbox has to be an existing file."""
    with pytest.raises(click.ClickException, match="nor an existing file"):
        cli._resolve_export_target(
            [str(tmp_path / "missing.tif")],
            "EPSG:4326",
            wsen=False,
        )


# ---------------------------------------------------------------------------
# The settings given to 'ivert options set'
# ---------------------------------------------------------------------------
def test_option_assignment_is_split_at_the_first_equals():
    """The key is trimmed and lowercased; the value is kept as given, '=' and all."""
    assert cli._parse_option_assignment(
        " ICESAT2_Classify_Workers =a=b",
        Config(),
    ) == ("icesat2_classify_workers", "a=b")


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("icesat2_classify_workers", "Invalid format"),
        ("user_configfile=x.ini", "read-only setting"),
        ("no_such_setting=1", "Unknown setting 'no_such_setting'"),
    ],
)
def test_bad_option_assignments_are_refused(assignment, message):
    """No '=', a read-only key, or an unknown one is a usage error."""
    with pytest.raises(click.UsageError, match=message):
        cli._parse_option_assignment(assignment, Config())


@pytest.mark.parametrize("value", ['"007"', "007", "7", " 8 "])
def test_an_nsidc_atl_version_is_checked_as_the_config_file_reads_it(value):
    """The quoted form is the one ivert_defaults.ini uses; the others work too."""
    assert cli._parse_option_assignment(f"nsidc_atl_version={value}", Config()) == (
        "nsidc_atl_version",
        value,
    )


@pytest.mark.parametrize("value", ["7x", "7.0", "1234", ""])
def test_a_bad_nsidc_atl_version_is_refused_when_set(value):
    """Saved, it would stop only the next download, with a traceback."""
    with pytest.raises(click.UsageError, match="nsidc_atl_version must be a number"):
        cli._parse_option_assignment(f"nsidc_atl_version={value}", Config())


def _user_config_path() -> Path:
    """The per-test user config file that conftest.py points IVERT at."""
    return Path(os.environ["IVERT_USER_CONFIG"])


def test_options_set_writes_the_user_config():
    """The value lands in the user config file's DEFAULT section."""
    cli._options_set_values(["icesat2_classify_workers=3"], assume_yes=True)

    written = configparser.ConfigParser(interpolation=None)
    written.read(_user_config_path())
    assert written["DEFAULT"]["icesat2_classify_workers"] == "3"


def test_one_bad_assignment_means_nothing_is_written():
    """Every pair is checked before any is saved."""
    with pytest.raises(click.UsageError, match="Unknown setting"):
        cli._options_set_values(
            ["icesat2_classify_workers=3", "no_such_setting=1"],
            assume_yes=True,
        )

    assert not _user_config_path().exists()
