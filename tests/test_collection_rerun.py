"""Tests for when re-running a collection validation skips, and what it redoes."""

import os

import h5py
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin

from ivert import validate_dem, validate_dem_collection

# The summary files of a collection run without a place name.
_SUMMARY_FILES = (
    "summary_results.h5",
    "summary_summary_stats.txt",
    "summary_plot.png",
    "summary_individual_results.csv",
)


def _make_tif(path):
    profile = {
        "driver": "GTiff",
        "width": 5,
        "height": 4,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_origin(-105.0, 40.4, 0.1, 0.1),
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(np.zeros((4, 5), dtype="float32"), 1)
    return str(path)


@pytest.fixture
def collection(tmp_path, monkeypatch):
    """Two DEMs and an output directory; validate_dem only records what it is given."""
    dems = [_make_tif(tmp_path / "a.tif"), _make_tif(tmp_path / "b.tif")]
    out = tmp_path / "out"
    out.mkdir()
    validated = []
    # Names of the DEMs the fake validate_dem() fails on.
    failing = set()

    def _fake_validate_dem(dem_path, *_args: object, **_kwargs: object):
        validated.append(dem_path)
        if os.path.basename(dem_path) in failing:
            msg = f"{dem_path} broke"
            raise ValueError(msg)

    class _FakeDatabase:
        def open_gdf(self):
            pass

    monkeypatch.setattr(
        validate_dem_collection.ivert.icesat2_database_v2,
        "IS2Database",
        _FakeDatabase,
    )
    monkeypatch.setattr(
        validate_dem_collection.validate_dem,
        "validate_dem",
        _fake_validate_dem,
    )
    # The summary outputs aren't under test here.
    for module, name in (
        (validate_dem_collection.validate_dem, "write_summary_stats_file"),
        (validate_dem_collection.plot_validation_results, "plot_histograms_and_line"),
        (validate_dem_collection.ivert.bathy_filters, "read_report_from_h5"),
        (validate_dem_collection.ivert.bathy_filters, "write_report_to_h5"),
        (validate_dem_collection.ivert.bathy_filters.BathyFilterReport, "combine"),
    ):
        monkeypatch.setattr(module, name, lambda *_args, **_kwargs: None)
    pd.DataFrame({"diff_mean": [1.0, 2.0], "numphotons_intd": [5, 6]}).to_hdf(
        out / "a_results.h5",
        key="results",
    )
    return tmp_path, out, dems, validated, failing


def _touch(directory, names):
    for name in names:
        (directory / name).write_text("")


def _run(tmp_path, out, overwrite=False):
    validate_dem_collection.validate_list_of_dems(
        str(tmp_path),
        output_dir=str(out),
        include_photon_validation=False,
        overwrite=overwrite,
    )


def test_rerun_validates_a_dem_missing_results_although_the_summaries_exist(
    collection,
):
    """A rerun once stopped as soon as the summary existed, so DEMs added later were never validated."""
    tmp_path, out, dems, validated, _ = collection
    _touch(out, _SUMMARY_FILES)

    _run(tmp_path, out)

    # The loop runs; validate_dem() itself reuses a.tif's results.
    assert validated == dems


def test_rerun_stops_when_every_dem_and_summary_is_done(collection):
    """The early stop still happens when nothing is left; an empty marker counts as done."""
    tmp_path, out, _, validated, _ = collection
    _touch(out, ("b_results_EMPTY.txt", *_SUMMARY_FILES))

    _run(tmp_path, out)

    assert validated == []


def test_rerun_rewrites_a_missing_summary(collection):
    """One missing summary file is enough to go round again; validate_dem() reuses finished DEMs."""
    tmp_path, out, dems, validated, _ = collection
    _touch(out, ("b_results_EMPTY.txt", *_SUMMARY_FILES[:-1]))

    _run(tmp_path, out)

    assert validated == dems


def test_dems_needing_validation_ignores_the_summary_files(tmp_path):
    """The collection's own summary_results.h5 must not be taken for a DEM's results."""
    dems = [_make_tif(tmp_path / "a.tif"), _make_tif(tmp_path / "b.tif")]
    out = tmp_path / "out"
    out.mkdir()
    _touch(out, ("a_results.h5", *_SUMMARY_FILES))

    assert validate_dem_collection.dems_needing_validation(str(tmp_path), str(out)) == (
        [dems[1]],
        [dems[0]],
    )


def test_a_failed_dem_gets_an_error_marker_and_is_not_retried(collection, caplog):
    """Without the marker a failing DEM is retried, and fails again, on every rerun.

    The marker records the error and traceback, and each rerun names the DEM
    as an error so the failure isn't forgotten.
    """
    tmp_path, out, dems, validated, failing = collection
    failing.add("b.tif")

    _run(tmp_path, out)

    marker = out / "b_results_ERROR.txt"
    text = marker.read_text()
    assert text.startswith(f"DEM: {dems[1]}\n")
    assert f"Error: {dems[1]} broke" in text
    assert "Traceback (most recent call last)" in text

    validated.clear()
    caplog.clear()
    _run(tmp_path, out)

    assert validated == [dems[0]]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors[0].startswith("Skipping b.tif: it failed in an earlier run")
    assert errors[-1].endswith(": b.tif")


def test_overwrite_deletes_the_error_marker_and_retries(collection):
    """-ow/--overwrite is how a failed DEM gets tried again."""
    tmp_path, out, dems, validated, _ = collection
    _touch(out, ["b_results_ERROR.txt"])

    _run(tmp_path, out, overwrite=True)

    assert validated == dems
    assert not (out / "b_results_ERROR.txt").exists()


def test_rerun_stops_with_a_failed_dem_and_reports_it(collection, caplog):
    """A failed DEM counts as done for the early stop, but the run still ends by naming it."""
    tmp_path, out, _, validated, _ = collection
    _touch(out, ("b_results_ERROR.txt", *_SUMMARY_FILES))

    _run(tmp_path, out)

    assert validated == []
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [
        (
            "1 of 2 DEMs failed in an earlier run and were not tried again (see their "
            "_results_ERROR.txt files; use -ow/--overwrite to retry them): b.tif"
        ),
    ]


def test_a_dem_that_cant_be_read_gets_an_error_marker(collection):
    """A DEM refused before validation starts gets a marker too, not only one that fails partway."""
    tmp_path, out, _, _, _ = collection
    with h5py.File(tmp_path / "c.h5", "w") as f:
        f["elev"] = np.zeros((4, 5), dtype="f4")
        f["uncert"] = np.zeros((4, 5), dtype="f4")

    _run(tmp_path, out)

    assert "has no georeferencing" in (out / "c_results_ERROR.txt").read_text()


def test_overwrite_deletes_an_empty_marker(tmp_path):
    """An old _results_EMPTY.txt used to be left beside new results, contradicting them."""
    results = tmp_path / "dem_results.h5"
    empty = tmp_path / "dem_results_EMPTY.txt"
    empty.write_text("")

    validate_dem._check_existing_outputs(
        str(tmp_path / "dem.tif"),
        str(results),
        str(empty),
        None,
        None,
        write_summary_stats=False,
        plot_results=False,
        location_name=None,
        overwrite=True,
        mark_empty_results=True,
        shared_ret_values={},
    )

    assert not empty.exists()
