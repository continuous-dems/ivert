"""How 'ivert validate' reads its options and inputs, and where it writes results."""

import math
from pathlib import Path

import click
import pytest

from ivert import cli
from ivert.photon_classes import BUILDINGS


# ---------------------------------------------------------------------------
# -c/--classes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("classes", ["1/40/41", "1,40,41", "1/40,41/", "/1//40/41"])
def test_class_codes_are_slash_or_comma_separated(classes):
    """Either separator works, and empty pieces are dropped."""
    assert cli._parse_class_codes(classes) == [1, 40, 41]


def test_a_class_code_that_is_not_a_number_is_an_error():
    """The bad value is named in the message."""
    with pytest.raises(click.ClickException, match="Invalid --classes value '1/sea'"):
        cli._parse_class_codes("1/sea")


def test_validation_classes_are_sorted_without_repeats():
    """The codes come back in order, each once."""
    assert cli._validation_classes("41/1/40/1", buildings=False) == [1, 40, 41]


def test_buildings_adds_the_building_class_once():
    """-b/--buildings adds the building class, unless it is already there."""
    assert cli._validation_classes("1/40", buildings=True) == sorted([1, 40, BUILDINGS])
    assert cli._validation_classes(f"1/{BUILDINGS}", buildings=True) == sorted(
        [1, BUILDINGS],
    )


@pytest.mark.parametrize("classes", ["", "/", ","])
def test_validation_needs_at_least_one_class(classes):
    """With no class code there are no photons to validate with."""
    with pytest.raises(click.ClickException, match="at least one photon class"):
        cli._validation_classes(classes, buildings=True)


# ---------------------------------------------------------------------------
# -p/--projection, -V/--vdatum, --ndv, --export-formats
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "projection",
    ["EPSG:4326", "EPSG:6893", "EPSG:4326+3855", "EPSG:4326+vdatum:mllw"],
)
def test_a_projection_with_or_without_a_vertical_datum_is_accepted(projection):
    """A horizontal CRS alone, a compound CRS, or one with a vertical datum added."""
    cli._check_projection_vdatum(projection)


def test_a_projection_with_an_unusable_vertical_datum_is_an_error():
    """The vertical part is checked once, before any DEM is validated."""
    with pytest.raises(click.ClickException, match="'vdatum:bogus' in -p/--projection"):
        cli._check_projection_vdatum("EPSG:4326+vdatum:bogus")


def test_no_vdatum_leaves_it_to_the_dem():
    """Without -V/--vdatum, None lets each DEM's own metadata give its datum."""
    assert cli._resolve_vdatum_option("NONE_PROVIDED") is None


def test_a_vdatum_name_becomes_a_reference():
    """A common name becomes a reference transformez takes."""
    assert cli._resolve_vdatum_option("navd88") == "EPSG:5703"


def test_an_unknown_vdatum_is_an_error():
    """An unknown name stops the run, pointing to the list of known names."""
    with pytest.raises(click.ClickException, match="--list-vdatums"):
        cli._resolve_vdatum_option("not-a-datum")


def test_no_ndv_leaves_it_to_the_dem():
    """Without --ndv, None lets the DEM's own nodata value stand."""
    assert cli._parse_ndv(None) is None


@pytest.mark.parametrize(("ndv", "expected"), [("-9999", -9999.0), ("0.5", 0.5)])
def test_an_ndv_is_a_number(ndv, expected):
    """A number is read as a float."""
    assert cli._parse_ndv(ndv) == expected


@pytest.mark.parametrize("ndv", ["nan", "NaN", "NAN"])
def test_an_ndv_of_nan_is_nan(ndv):
    """'nan', in any case, is NaN."""
    assert math.isnan(cli._parse_ndv(ndv))


def test_an_ndv_that_is_not_a_number_is_an_error():
    """The bad value is named in the message."""
    with pytest.raises(click.ClickException, match="Invalid --ndv value 'none'"):
        cli._parse_ndv("none")


def test_no_export_formats_uses_the_config_default():
    """None tells validate_dem to use the configured formats."""
    assert cli._export_formats_override(None) is None


@pytest.mark.parametrize("value", ["none", "NONE", " None ", ""])
def test_export_formats_of_none_skips_error_exports(value):
    """'none' or an empty value means no error exports for this run."""
    assert cli._export_formats_override(value) == []


def test_export_formats_are_passed_on():
    """Any other value goes on to validate_dem as it is."""
    assert cli._export_formats_override("gpkg,shp") == "gpkg,shp"


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------
def test_a_quoted_glob_pattern_is_expanded(tmp_path):
    """A pattern the shell didn't expand is expanded here."""
    for name in ("a.tif", "b.tif", "c.txt"):
        (tmp_path / name).touch()

    expanded = cli._expand_inputs((str(tmp_path / "*.tif"),))

    assert sorted(Path(f).name for f in expanded) == ["a.tif", "b.tif"]


def test_inputs_matching_nothing_are_kept_as_given(tmp_path):
    """A name that matches nothing is passed on, to fail with a clear error later."""
    missing = str(tmp_path / "missing.tif")

    assert cli._expand_inputs((missing, "NETCDF:x.nc:elev")) == [
        missing,
        "NETCDF:x.nc:elev",
    ]


def test_no_inputs_is_an_error():
    """There has to be something to validate."""
    with pytest.raises(click.ClickException, match="No input files"):
        cli._expand_inputs(())


# ---------------------------------------------------------------------------
# The output directory
# ---------------------------------------------------------------------------
def test_an_absolute_outdir_is_used_as_it_is(tmp_path):
    """An absolute -o/--outdir doesn't depend on where the DEMs are."""
    outdir = tmp_path / "results"

    assert cli._results_dir(str(tmp_path / "dems" / "dem.tif"), outdir) == outdir


def test_a_relative_outdir_is_beside_a_dem_file(tmp_path, monkeypatch):
    """A relative outdir is in the DEM's folder, not the working directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dems").mkdir()

    assert cli._results_dir("dems/dem.tif", Path("results")) == (
        tmp_path / "dems" / "results"
    )


def test_a_relative_outdir_is_inside_a_dem_directory(tmp_path):
    """Given a directory of DEMs, a relative outdir is inside it."""
    dems = tmp_path / "dems"
    dems.mkdir()

    assert cli._results_dir(str(dems), Path("results")) == dems / "results"


def test_a_relative_outdir_is_beside_the_first_dem_of_a_list(tmp_path):
    """For several DEMs, a relative outdir is in the first one's folder."""
    first = tmp_path / "a" / "dem1.tif"
    second = tmp_path / "b" / "dem2.tif"

    assert cli._results_dir([str(first), str(second)], Path("results")) == (
        tmp_path / "a" / "results"
    )


def test_a_relative_outdir_is_beside_a_subdataset_file(tmp_path):
    """A subdataset string's outdir is beside its file, not named after the string."""
    dem = f'NETCDF:"{tmp_path / "dem.nc"}":elevation'

    assert cli._results_dir(dem, Path("results")) == tmp_path / "results"
