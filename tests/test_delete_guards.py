"""'ivert cache delete' and 'ivert database delete' refuse folders that aren't IVERT's.

An empty or relative folder setting, which a user config file can hold, names a
folder under wherever IVERT is run, and clearing it would delete the user's files.
"""

import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from ivert import cli
from ivert.utils.configfile import Config


def _set_user_config(**settings: str) -> None:
    """Write these settings into the per-test user config file, as a user might."""
    lines = ["[DEFAULT]", *(f"{key} = {value}" for key, value in settings.items())]
    Path(os.environ["IVERT_USER_CONFIG"]).write_text("\n".join(lines) + "\n")


def _run(*args: str):
    return CliRunner().invoke(cli.ivert_cli, list(args))


@pytest.mark.parametrize("value", ["", ".", "cache"])
def test_cache_delete_refuses_a_folder_relative_to_where_it_is_run(
    tmp_path,
    monkeypatch,
    value,
):
    """The files in the current folder are left alone."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cache").mkdir()
    keep = [tmp_path / "mine.txt", tmp_path / "cache" / "mine.txt"]
    for path in keep:
        path.write_text("keep me")
    _set_user_config(cache_directory=value)

    result = _run("cache", "delete", "-y")

    assert result.exit_code == 1
    assert "relative to wherever IVERT is run" in result.output
    assert all(path.exists() for path in keep)


def test_cache_delete_refuses_the_home_folder(tmp_path, monkeypatch):
    """A cache set to the home folder itself would take everything in it."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "mine.txt").write_text("keep me")
    monkeypatch.setenv("HOME", str(home))
    _set_user_config(cache_directory=str(home))

    result = _run("cache", "delete", "-y")

    assert result.exit_code == 1
    assert "won't clear it" in result.output
    assert (home / "mine.txt").exists()


def test_cache_delete_clears_a_cache_given_by_its_full_path(tmp_path):
    """The check doesn't get in the way of the usual case."""
    cache = tmp_path / "cache"
    (cache / "icesat2").mkdir(parents=True)
    (cache / "icesat2" / "granule.h5").write_text("")
    (cache / "loose.txt").write_text("")
    _set_user_config(cache_directory=str(cache))

    result = _run("cache", "delete", "-y")

    assert result.exit_code == 0, result.output
    assert cache.is_dir()
    assert not any(cache.iterdir())


def test_database_delete_refuses_an_empty_database_folder(tmp_path, monkeypatch):
    """With --all it deletes every .nc file there, a user's NetCDF DEMs among them."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dem.nc").write_text("keep me")
    _set_user_config(ivert_database_directory="")

    result = _run("database", "delete", "--all", "-y")

    assert result.exit_code == 1
    assert "relative to wherever IVERT is run" in result.output
    assert (tmp_path / "dem.nc").exists()


@pytest.mark.parametrize("value", ["", "  "])
def test_options_refuses_an_empty_path(value):
    """Saved, it would name the folder IVERT is run from."""
    with pytest.raises(click.UsageError, match="'cache_directory' is a path"):
        cli._parse_option_assignment(f"cache_directory={value}", Config())


def test_options_refuses_an_empty_path_even_if_already_empty():
    """A setting stays a path although a user config has already emptied it."""
    _set_user_config(cache_directory="")

    with pytest.raises(click.UsageError, match="'cache_directory' is a path"):
        cli._parse_option_assignment("cache_directory=", Config())


def test_options_lets_a_setting_that_may_be_blank_be_emptied():
    """An empty bathy_ref_raster, its default, means no reference raster."""
    assert cli._parse_option_assignment("bathy_ref_raster=", Config()) == (
        "bathy_ref_raster",
        "",
    )
