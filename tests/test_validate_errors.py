"""Tests that 'ivert validate' stops on a bad input with an error, not a traceback."""

import logging

import click
import numpy as np
import pytest
import rasterio
import rasterio.transform

from ivert import cli, validate_dem, validate_dem_collection
from ivert.utils.dem_source import DEMCRSError


def _write_tif(path, crs="EPSG:4326"):
    """Write a 4x5 GeoTIFF with a horizontal CRS and no vertical datum."""
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


# ---------------------------------------------------------------------------
# A DEM with no vertical datum
# ---------------------------------------------------------------------------
def test_a_dem_without_a_vertical_datum_fails_before_the_sub_process(
    tmp_path,
    monkeypatch,
):
    """The error was raised in the sub-process, reaching the caller as a RuntimeError.

    That printed two tracebacks, the sub-process's and then "exited with exitcode 1".
    """

    def no_sub_process(*_args: object, **_kwargs: object):
        pytest.fail("validate_dem() started a sub-process.")

    monkeypatch.setattr(validate_dem.mp, "Process", no_sub_process)
    dem = _write_tif(tmp_path / "dem.tif")

    with pytest.raises(DEMCRSError, match="-V/--vdatum"):
        validate_dem.validate_dem(dem, tmp_path / "out")


def test_a_collection_skips_a_dem_error_without_a_traceback(
    tmp_path,
    monkeypatch,
    caplog,
):
    """The error names the DEM and the fix, so a traceback adds nothing."""

    def fail(*_args: object, **_kwargs: object):
        msg = "dem.tif has no vertical datum in its metadata."
        raise DEMCRSError(msg)

    monkeypatch.setattr(validate_dem_collection.validate_dem, "validate_dem", fail)
    marker = tmp_path / "dem_ERROR.txt"

    with caplog.at_level(logging.ERROR):
        result = validate_dem_collection._run_validate_dem(
            str(tmp_path / "dem.tif"),
            "dem.tif",
            marker,
            {},
        )

    assert result is None
    assert "Skipping: dem.tif has no vertical datum" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "Error: dem.tif has no vertical datum" in marker.read_text()
    assert "Traceback" not in marker.read_text()


# ---------------------------------------------------------------------------
# A missing input
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["nothing.tif", "dems/nothing*.tif"])
def test_a_single_missing_input_is_a_clear_error(tmp_path, name):
    """It reached the collection code, which raised FileNotFoundError."""
    with pytest.raises(click.ClickException, match="does not exist"):
        cli._check_single_input_exists([str(tmp_path / name)])


def test_a_single_existing_input_passes(tmp_path):
    """A file, a folder, or a subdataset string naming a file that exists."""
    dem = _write_tif(tmp_path / "dem.tif")

    cli._check_single_input_exists([dem])
    cli._check_single_input_exists([str(tmp_path)])
    cli._check_single_input_exists([f'NETCDF:"{dem}":elev'])


def test_several_inputs_are_left_to_the_collection(tmp_path):
    """The collection skips each input it can't read, so one typo doesn't stop all."""
    cli._check_single_input_exists(
        [str(tmp_path / "a.tif"), str(tmp_path / "b.tif")],
    )
