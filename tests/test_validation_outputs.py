"""The steps between a DEM's photons and its output files, and reading those back."""

import logging

import pandas as pd
import pytest

from ivert import bathy_filters, plot_validation_results, validate_dem


# ---------------------------------------------------------------------------
# Exclusion zones
# ---------------------------------------------------------------------------
def _photons():
    """Three photons on a diagonal, in DEM space, one inside the box 1-2 x 1-2."""
    return pd.DataFrame(
        {"dem_x": [0.5, 1.5, 2.5], "dem_y": [0.5, 1.5, 2.5], "z": [1.0, 2.0, 3.0]},
    )


def test_photons_inside_an_exclusion_zone_are_dropped(caplog):
    """A (minx, miny, maxx, maxy) zone removes the photons inside it, and says so."""
    with caplog.at_level(logging.INFO):
        kept = validate_dem._drop_excluded_photons(
            _photons(),
            [(1.0, 1.0, 2.0, 2.0)],
            "EPSG:4326",
        )

    assert kept["z"].tolist() == [1.0, 3.0]
    assert "1 photons excluded by exclusion zone(s)." in caplog.text


def test_an_exclusion_zone_with_no_photons_in_it_changes_nothing(caplog):
    """Nothing is logged when nothing is dropped."""
    with caplog.at_level(logging.INFO):
        kept = validate_dem._drop_excluded_photons(
            _photons(),
            [(10.0, 10.0, 11.0, 11.0)],
            "EPSG:4326",
        )

    assert kept["z"].tolist() == [1.0, 2.0, 3.0]
    assert "excluded" not in caplog.text


# ---------------------------------------------------------------------------
# The bathymetry filters only run when they have something to do
# ---------------------------------------------------------------------------
def test_bathy_filters_skip_a_dem_with_no_photons():
    """No photons: nothing to filter, and no report."""
    assert validate_dem._filter_bathy_photons(None, (1, 40), None, None, None) == (
        None,
        None,
    )


def test_bathy_filters_skip_a_validation_without_the_bathy_class():
    """The photons pass through untouched when bathymetry wasn't asked for."""
    classes = (1, 6)
    assert bathy_filters.BATHY_FLOOR_CLASS not in classes
    fetch_result = object()

    got, report = validate_dem._filter_bathy_photons(
        fetch_result,
        classes,
        None,
        None,
        None,
    )

    assert got is fetch_result
    assert report is None


# ---------------------------------------------------------------------------
# Outliers
# ---------------------------------------------------------------------------
_DIFFS = pd.DataFrame({"diff_mean": [0.0, 0.1, -0.1, 0.05, 100.0]})


def test_outliers_beyond_the_threshold_are_removed():
    """At 1 SD the 100 m error is dropped; the rest are kept."""
    kept = validate_dem._remove_outliers(_DIFFS, 1.0)

    assert kept["diff_mean"].tolist() == [0.0, 0.1, -0.1, 0.05]


def test_a_wide_threshold_keeps_every_cell():
    """With five cells no error is 2.5 SD from the mean, not even the 100 m one."""
    assert len(validate_dem._remove_outliers(_DIFFS, 2.5)) == len(_DIFFS)


def test_the_outlier_threshold_must_be_a_number():
    """A string threshold would otherwise fail deep inside pandas."""
    with pytest.raises(TypeError, match="must be a number"):
        validate_dem._remove_outliers(_DIFFS, "2.5")


# ---------------------------------------------------------------------------
# The file that marks a DEM with no results
# ---------------------------------------------------------------------------
def test_empty_results_marker_is_written_and_reported(tmp_path, caplog):
    """The file is written, exported, and handed back to the parent process."""
    marker = tmp_path / "dem_EMPTY.txt"
    shared: dict = {}
    files: list = []

    with caplog.at_level(logging.INFO):
        validate_dem._write_empty_results_marker(
            marker,
            "dem.tif had no ICESat-2 results.",
            "valid ICESat-2 data",
            shared_ret_values=shared,
            files_to_export=files,
        )

    assert marker.read_text(encoding="utf-8") == "dem.tif had no ICESat-2 results."
    assert files == [marker]
    assert shared == {"empty_results_filename": marker}
    assert "to indicate no valid ICESat-2 data was returned here" in caplog.text


def test_no_empty_results_marker_when_none_is_wanted():
    """With no file name (--no-mark-empty), nothing is written or recorded."""
    shared: dict = {}
    files: list = []

    validate_dem._write_empty_results_marker(
        None,
        "text",
        "data",
        shared_ret_values=shared,
        files_to_export=files,
    )

    assert shared == {}
    assert files == []


# ---------------------------------------------------------------------------
# Reading results files back
# ---------------------------------------------------------------------------
def _results_file(path, value):
    pd.DataFrame({"diff_mean": [value]}).to_hdf(path, key="results")
    return str(path)


def test_one_results_file_is_labelled_with_its_own_name(tmp_path):
    """Without the DEM's name, the results file's name is the best label there is."""
    path = _results_file(tmp_path / "dem_results.h5", 1.0)

    data = plot_validation_results.get_data_from_h5_or_list(
        path,
        include_filenames=True,
    )

    assert data["filename"].tolist() == ["dem_results.h5"]


def test_results_files_are_not_labelled_unless_asked(tmp_path):
    """The 'filename' column is only added on request."""
    path = _results_file(tmp_path / "dem_results.h5", 1.0)

    data = plot_validation_results.get_data_from_h5_or_list(path)

    assert "filename" not in data.columns


def test_missing_results_files_in_a_list_are_skipped(tmp_path):
    """A DEM with no results has no results file; the others are still read."""
    files = [
        _results_file(tmp_path / "a.h5", 1.0),
        str(tmp_path / "missing.h5"),
        _results_file(tmp_path / "b.h5", 2.0),
    ]

    data = plot_validation_results.get_data_from_h5_or_list(
        files,
        orig_filenames=["a.tif", "missing.tif", "b.tif"],
        include_filenames=True,
    )

    assert data["filename"].tolist() == ["a.tif", "b.tif"]
    assert data["diff_mean"].tolist() == [1.0, 2.0]


@pytest.mark.parametrize(
    ("results", "orig_filenames", "message"),
    [
        ("one.h5", ["a.tif"], "must be a string"),
        (["one.h5"], 42, "must be a list"),
    ],
)
def test_dem_names_must_match_the_results_given(results, orig_filenames, message):
    """One results file takes one DEM name; a list of them takes a list."""
    with pytest.raises(TypeError, match=message):
        plot_validation_results.get_data_from_h5_or_list(
            results,
            orig_filenames=orig_filenames,
            include_filenames=True,
        )
