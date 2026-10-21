"""Tests for run manifests: writing them, reading them back, and replaying them."""

import click
import pytest

from ivert import manifest
from ivert.cli import validate

_TRACKED = ("band_num", "projection", "variable")


def _reconcile(
    manifest_options,
    *,
    interactive=False,
    current_values=None,
    command_line=(),
):
    return manifest.reconcile_options(
        "old.ini",
        "0.6.10",
        manifest_options,
        _TRACKED,
        "0.6.11",
        interactive=interactive,
        current_values=current_values,
        command_line=command_line,
    )


def test_missing_options_warn_only_when_they_fall_back_to_defaults(caplog):
    """A manifest from before an option existed still replays.

    The option takes its command-line value quietly, or its default with a
    warning. Adding -p and --variable once made every older manifest
    incompatible.
    """
    options = _reconcile(
        {"band_num": "2"},
        current_values={"band_num": 1, "projection": "EPSG:4326", "variable": None},
        command_line={"projection"},
    )

    assert options == {"band_num": "2"}
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [
        (
            "old.ini: the manifest (IVERT 0.6.10) has no 'variable' option; "
            "using its default (not set)."
        ),
    ]


def test_unrecognized_options_stop_without_a_terminal():
    """Dropping an option IVERT doesn't recognize could change results.

    A run that can't ask the user stops instead.
    """
    with pytest.raises(click.ClickException, match="doesn't recognize"):
        _reconcile({"band_num": "1", "projection": "", "variable": "", "old": "x"})


def test_unrecognized_options_are_dropped_if_the_user_agrees(monkeypatch):
    """At a terminal the user is asked; agreeing replays the manifest without them."""
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: True)

    options = _reconcile(
        {"band_num": "1", "projection": "", "variable": "", "old": "x"},
        interactive=True,
    )

    assert options == {"band_num": "1", "projection": "", "variable": ""}


def test_write_manifest_through_a_symlinked_folder(tmp_path):
    """Writing through "symlink/../out" once crashed.

    The temporary file went in a different folder from the manifest.
    """
    (tmp_path / "real").mkdir()
    (tmp_path / "dems").mkdir()
    (tmp_path / "dems" / "link").symlink_to(tmp_path / "real")
    path = tmp_path / "dems" / "link" / ".." / "out" / manifest.MANIFEST_FILENAME

    manifest.write_manifest(path, "0.7.0", {"band_num": 1}, {"outdir": "out"})

    # ".." after a symlink leads out of the symlink's target, as the system resolves it.
    written = tmp_path / "out" / manifest.MANIFEST_FILENAME
    assert manifest.read_manifest(written)[1] == {"band_num": "1"}
    assert list(written.parent.iterdir()) == [written]


# --- Where manifests go, and what they record --------------------------------------


def test_a_collection_run_gets_one_manifest(tmp_path):
    """A run over several DEMs writes 'ivert_manifest.ini' in its output folder."""
    assert manifest.manifest_path(tmp_path) == tmp_path / "ivert_manifest.ini"


@pytest.mark.parametrize(
    ("dem_name", "filename"),
    [
        ("/data/dems/tile_a.tif", "tile_a_ivert_manifest.ini"),
        ('NETCDF:"/data/dems/tile_a.nc":elev', "tile_a_elev_ivert_manifest.ini"),
    ],
)
def test_single_dem_runs_get_a_manifest_named_for_the_dem(tmp_path, dem_name, filename):
    """Single-DEM runs, or two variables of one file, don't share a manifest."""
    assert manifest.manifest_path(tmp_path, dem_name=dem_name) == tmp_path / filename


def test_inputs_and_output_folder_are_not_recorded_as_options():
    """The DEMs, -o, --overwrite and -m itself aren't settings that change results."""
    tracked = manifest.tracked_params(validate)
    assert not set(tracked) & manifest.UNTRACKED_PARAMS
    assert {"band_num", "vdatum", "exclude", "include_photons"} <= set(tracked)


# --- Round trips ---------------------------------------------------------------------

# Values for some of 'ivert validate's options, one of each type it uses.
_CHANGED_VALUES = {
    "vdatum": "mllw",
    "region_name": "Test Bay",
    "include_photons": True,
    "measure_coverage": False,
    "minimum_coverage_pct": 12.5,
    "band_num": 2,
    "projection": "EPSG:4326+3855",
    "min_photons": 5,
    "bathy_ref_raster": "/data/100% ref.tif",
    "exclude": ("zone_a.shp", "zone_b.gpkg"),
}


def _validate_values(changed=None):
    """The tracked options' values for an 'ivert validate' run, with some changed."""
    ctx = validate.make_context("validate", ["dem.tif"], resilient_parsing=True)
    values = {name: ctx.params[name] for name in manifest.tracked_params(validate)}
    return ctx, {**values, **(changed or {})}


@pytest.mark.parametrize(
    "changed",
    [None, _CHANGED_VALUES],
    ids=["defaults", "changed"],
)
def test_a_written_manifest_replays_the_same_options(tmp_path, changed):
    """Every option read back from a manifest has the value that was written."""
    ctx, values = _validate_values(changed)
    path = tmp_path / manifest.MANIFEST_FILENAME
    manifest.write_manifest(path, "0.7.1", values, {"outdir": "out"})

    version, raw = manifest.read_manifest(path)
    params = manifest.tracked_params(validate)
    replayed = {
        name: manifest.parse_option(ctx, params[name], raw[name], path) for name in raw
    }
    assert version == "0.7.1"
    assert replayed == values


def test_a_manifest_reads_back_as_the_text_it_was_written_from(tmp_path):
    """A rerun compares settings as text, so unchanged ones must read back the same.

    Otherwise a rerun warns that settings changed when they didn't.
    """
    _ctx, values = _validate_values(_CHANGED_VALUES)
    values["Mixed_Case"] = "kept as written"
    path = tmp_path / manifest.MANIFEST_FILENAME
    manifest.write_manifest(path, "0.7.1", values, {})
    assert manifest.read_manifest_options(path) == {
        name: manifest.format_value(value) for name, value in values.items()
    }


def test_a_blank_value_means_the_options_default(tmp_path):
    """A blank entry runs with the option's own default, as if it weren't set."""
    ctx, _values = _validate_values()
    params = manifest.tracked_params(validate)
    assert manifest.parse_option(ctx, params["band_num"], "  ", tmp_path) == 1
    assert manifest.parse_option(ctx, params["exclude"], "", tmp_path) == ()


@pytest.mark.parametrize(
    ("name", "raw"),
    [("min_photons", "0"), ("band_num", "two"), ("minimum_coverage_pct", "150")],
)
def test_a_value_the_option_rejects_names_the_option_and_manifest(tmp_path, name, raw):
    """A manifest value is checked as strictly as the same value on the command line."""
    ctx, _values = _validate_values()
    path = tmp_path / "run.ini"
    with pytest.raises(click.ClickException, match=f"'{name}' in manifest '.*run.ini'"):
        manifest.parse_option(ctx, manifest.tracked_params(validate)[name], raw, path)


# --- Files that aren't manifests -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (None, "Could not read manifest"),
        ("this is not an INI file\n", "Could not read manifest"),
        ("[options]\nband_num = 1\n", "no 'version' in an \\[ivert\\] section"),
        ("[ivert]\nversion = 0.7.1\n", "no \\[options\\] section"),
    ],
    ids=["missing", "not INI", "no version", "no options"],
)
def test_a_file_that_isnt_a_manifest_is_reported(tmp_path, text, match):
    """read_manifest() says what is wrong; read_manifest_options() gives None."""
    path = tmp_path / "run.ini"
    if text is not None:
        path.write_text(text)
    with pytest.raises(click.ClickException, match=match):
        manifest.read_manifest(path)
    assert manifest.read_manifest_options(path) is None


# --- Manifests from other versions ---------------------------------------------------


def test_unrecognized_options_stop_the_run_if_the_user_declines(monkeypatch):
    """Answering no at the prompt stops instead of running without them."""
    monkeypatch.setattr(click, "confirm", lambda *_args, **_kwargs: False)
    with pytest.raises(click.ClickException, match="incompatible"):
        _reconcile(
            {"band_num": "1", "projection": "", "variable": "", "old": "x"},
            interactive=True,
        )


@pytest.mark.parametrize(
    ("manifest_version", "current_version", "says"),
    [
        ("0.6.10", "0.7.1", "belongs to a previous version of IVERT"),
        ("0.8.0", "0.7.1", "Upgrade IVERT"),
        ("0.7.1", "0.7.1.dev3", "is incompatible with the current version"),
        ("not-a-version", "0.7.1", "is incompatible with the current version"),
    ],
)
def test_the_warning_says_whether_the_manifest_is_older_or_newer(
    caplog,
    manifest_version,
    current_version,
    says,
):
    """An older manifest is explained as old; a newer one says to upgrade IVERT."""
    with pytest.raises(click.ClickException):
        manifest.reconcile_options(
            "run.ini",
            manifest_version,
            {"band_num": "1", "old": "x"},
            ("band_num",),
            current_version,
            interactive=False,
        )
    assert says in caplog.text
    assert "doesn't recognize: old" in caplog.text


def test_a_failed_write_keeps_the_old_manifest(tmp_path, monkeypatch):
    """A write that fails part-way leaves the old manifest whole, and no temp file."""
    path = tmp_path / manifest.MANIFEST_FILENAME
    manifest.write_manifest(path, "0.7.0", {"band_num": 1}, {})
    before = path.read_text()

    def broken_replace(_self: object, _target: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(manifest.Path, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        manifest.write_manifest(path, "0.7.1", {"band_num": 2}, {})
    assert path.read_text() == before
    assert list(tmp_path.iterdir()) == [path]
