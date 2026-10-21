"""Tests for a collection's summary CSV, and for choosing which DEMs it runs over."""

import math

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin

from ivert import validate_dem, validate_dem_collection
from ivert.validate_dem_collection import write_summary_csv_file

# Two DEMs' cell results: four cells of a.tif, one of b.tif.
RESULTS = pd.DataFrame(
    {
        "filename": ["a.tif"] * 4 + ["b.tif"],
        "diff_mean": [1.0, -1.0, 2.0, -2.0, 0.5],
        "numphotons_intd": [3, 5, 4, 4, 9],
    },
)


def _make_tif(path):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(-105.0, 40.2, 0.1, 0.1),
    ) as ds:
        ds.write(np.zeros((2, 2), dtype="float32"), 1)
    return str(path)


# --- write_summary_csv_file ---------------------------------------------------------


def test_each_dem_gets_a_row_of_its_own_statistics(tmp_path):
    """Each DEM's row is computed from its own cells only."""
    out = write_summary_csv_file(RESULTS, [], tmp_path / "summary.csv")
    a = out.set_index("filename").loc["a.tif"]
    assert a["rmse"] == pytest.approx(math.sqrt(10 / 4))
    assert a["mean_bias"] == pytest.approx(0.0)
    assert a["stddev_from_mean"] == pytest.approx(np.std([1, -1, 2, -2], ddof=1))
    assert a["n_cells_validated"] == 4
    assert a["mean_photons_per_cell"] == pytest.approx(4.0)


def test_the_rmse_matches_the_dems_own_summary_file(tmp_path):
    """The CSV and a DEM's summary_stats.txt report one RMSE, not two."""
    a_cells = RESULTS[RESULTS["filename"] == "a.tif"].assign(
        numphotons_bathy=0,
        stddev=0.1,
    )
    stats_file = tmp_path / "a_summary_stats.txt"
    validate_dem.write_summary_stats_file(a_cells, stats_file)
    rmse_line = next(
        line for line in stats_file.read_text().splitlines() if line.startswith("RMSE")
    )

    out = write_summary_csv_file(RESULTS, [], tmp_path / "summary.csv")
    rmse = out.set_index("filename").loc["a.tif", "rmse"]
    # The summary file gives it to three decimal places.
    assert rmse_line.split(": ")[1] == f"{rmse:.3f}"


def test_a_dem_with_one_validated_cell_has_its_rmse(tmp_path):
    """One cell is an RMSE of its own error; it used to divide by zero."""
    out = write_summary_csv_file(RESULTS, [], tmp_path / "summary.csv")
    b = out.set_index("filename").loc["b.tif"]
    assert b["rmse"] == pytest.approx(0.5)
    assert b["n_cells_validated"] == 1
    assert np.isnan(b["stddev_from_mean"])


def test_dems_with_no_photons_get_a_row_without_statistics(tmp_path):
    """They come after the validated DEMs, with no cells and no statistics."""
    out = write_summary_csv_file(RESULTS, ["c.tif"], tmp_path / "summary.csv")
    assert list(out["filename"]) == ["a.tif", "b.tif", "c.tif"]
    c = out.set_index("filename").loc["c.tif"]
    assert c["n_cells_validated"] == 0
    assert (
        c[["rmse", "mean_bias", "stddev_from_mean", "mean_photons_per_cell"]]
        .isna()
        .all()
    )


def test_the_csv_holds_what_is_returned(tmp_path):
    """The file written has the same rows and values as the table returned."""
    path = tmp_path / "summary.csv"
    out = write_summary_csv_file(RESULTS, ["c.tif"], path)
    pd.testing.assert_frame_equal(pd.read_csv(path), out, check_dtype=False)


def test_the_results_can_come_from_an_h5_file(tmp_path):
    """A results file name works as well as a table."""
    h5 = tmp_path / "summary_results.h5"
    RESULTS.to_hdf(h5, key="results")
    from_file = write_summary_csv_file(str(h5), [], tmp_path / "from_file.csv")
    from_table = write_summary_csv_file(RESULTS, [], tmp_path / "from_table.csv")
    pd.testing.assert_frame_equal(from_file, from_table)


def test_results_without_filenames_are_refused(tmp_path):
    """Without a 'filename' column the cells can't be told apart by DEM."""
    with pytest.raises(ValueError, match="'filename' column"):
        write_summary_csv_file(RESULTS.drop(columns="filename"), [], tmp_path / "s.csv")
    with pytest.raises(TypeError, match="DataFrame or a file name"):
        write_summary_csv_file(RESULTS.to_numpy(), [], tmp_path / "s.csv")


# --- Which DEMs a collection runs over ----------------------------------------------


def test_a_one_item_list_is_read_as_that_item(tmp_path):
    """['dems/'] is the directory, not a list holding one name."""
    dems = [_make_tif(tmp_path / "a.tif"), _make_tif(tmp_path / "b.tif")]
    out = tmp_path / "out"
    to_validate, reused = validate_dem_collection.dems_needing_validation(
        [str(tmp_path)],
        str(out),
    )
    assert (to_validate, reused) == (dems, [])


def test_a_dem_that_doesnt_exist_is_an_error(tmp_path):
    """A single DEM path that isn't there is reported, not silently skipped."""
    with pytest.raises(FileNotFoundError, match="does not exist"):
        validate_dem_collection.dems_needing_validation(
            str(tmp_path / "missing.tif"),
            str(tmp_path),
        )


def test_overwrite_validates_every_dem_again(tmp_path):
    """With overwrite, a DEM's existing results don't make it reused."""
    dems = [_make_tif(tmp_path / "a.tif"), _make_tif(tmp_path / "b.tif")]
    out = tmp_path / "out"
    out.mkdir()
    (out / "a_results.h5").write_text("")
    assert validate_dem_collection.dems_needing_validation(str(tmp_path), str(out)) == (
        [dems[1]],
        [dems[0]],
    )
    assert validate_dem_collection.dems_needing_validation(
        str(tmp_path),
        str(out),
        overwrite=True,
    ) == (dems, [])
