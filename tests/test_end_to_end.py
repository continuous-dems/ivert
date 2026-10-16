"""Real validations of small DEMs, from photon database to output files, offline.

Nothing is faked: the photons, landmask and reference bathymetry come from the
synthetic_region fixture (see conftest.py), and validate_dem() and
validate_list_of_dems() run as they do for a user. Collections write into a results
folder beside the DEMs, as 'ivert validate' does by default.
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ivert import validate_dem, validate_dem_collection

# The summary files of a collection run without a place name.
_SUMMARY_FILES = {
    "summary_results.h5",
    "summary_summary_stats.txt",
    "summary_plot.png",
    "summary_individual_results.csv",
}


def _files_in(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()}


def _validate_collection(dem_dir: Path, **kwargs: object) -> list | None:
    """Validate every DEM in dem_dir into dem_dir/ivert_results, as the CLI would."""
    return validate_dem_collection.validate_list_of_dems(
        str(dem_dir),
        output_dir=dem_dir / "ivert_results",
        **kwargs,
    )


def test_a_dem_is_validated_against_the_photons_under_it(synthetic_region, tmp_path):
    """Each cell is compared with its photons; a DEM 1 m high shows a 1 m error."""
    dem = synthetic_region.dem(
        "land.tif",
        values=np.full((3, 3), synthetic_region.LAND_Z + 1.0),
    )
    out = tmp_path / "out"
    out.mkdir()

    validate_dem.validate_dem(str(dem), out, classes=(1,))

    assert {
        "land_results.h5",
        "land_summary_stats.txt",
        "land_plot.png",
        "land_errors.tif",
    } <= _files_in(out)
    results = validate_dem.read_dataframe_file(out / "land_results.h5")
    assert len(results) == 9
    assert results["diff_mean"].to_numpy() == pytest.approx(np.ones(9), abs=0.1)


def test_a_dem_with_no_photons_under_it_is_marked_empty(synthetic_region, tmp_path):
    """A DEM outside the photons gets an empty-results marker and no results file."""
    dem = synthetic_region.dem("far.tif", corner=(-104.2, 39.2))
    out = tmp_path / "out"
    out.mkdir()

    validate_dem.validate_dem(str(dem), out, classes=(1,), mark_empty_results=True)

    assert _files_in(out) == {"far_results_EMPTY.txt"}


def test_a_collection_validates_land_and_seafloor_dems(synthetic_region):
    """A land and a seafloor DEM each get their results, and the run a summary."""
    land = synthetic_region.dem("land.tif")
    synthetic_region.dem("sea.tif", corner=synthetic_region.SEA_DEM)

    written = _validate_collection(land.parent)

    # Every output goes in the results folder; the DEMs' folder gets nothing else.
    assert _files_in(land.parent) == {"land.tif", "sea.tif", "ivert_results"}
    out = land.parent / "ivert_results"
    files = _files_in(out)
    assert {"land_results.h5", "sea_results.h5"} <= files
    assert files >= _SUMMARY_FILES
    assert written is not None
    summary = pd.read_csv(out / "summary_individual_results.csv")
    assert sorted(Path(f).name for f in summary["filename"]) == ["land.tif", "sea.tif"]
    assert summary["n_cells_validated"].tolist() == [9, 9]


def test_a_collection_rerun_reuses_finished_work(synthetic_region):
    """A second run finds nothing to do; -ow validates every DEM again."""
    dem = synthetic_region.dem("land.tif")
    results = dem.parent / "ivert_results" / "land_results.h5"
    _validate_collection(dem.parent)
    first_mtime = results.stat().st_mtime_ns

    assert _validate_collection(dem.parent) is None
    assert results.stat().st_mtime_ns == first_mtime

    assert _validate_collection(dem.parent, overwrite=True)
    assert results.stat().st_mtime_ns != first_mtime


def test_a_collection_marks_a_dem_that_fails_and_validates_the_rest(synthetic_region):
    """A DEM with no CRS gets an error marker; the others still get results."""
    good = synthetic_region.dem("land.tif")
    synthetic_region.dem("no_crs.tif", corner=synthetic_region.SEA_DEM, crs=None)

    _validate_collection(good.parent)

    files = _files_in(good.parent / "ivert_results")
    assert "no_crs_results_ERROR.txt" in files
    assert "land_results.h5" in files
    assert files >= _SUMMARY_FILES
