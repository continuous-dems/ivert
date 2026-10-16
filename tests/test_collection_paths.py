"""Where a collection run puts its summary files, and what it names them."""

from pathlib import Path

from ivert import validate_dem_collection


def _out_dir(dem_input, output_dir=None, fname_filter=None, fname_omit=None) -> Path:
    return validate_dem_collection._collection_output_dir(
        dem_input,
        output_dir,
        fname_filter,
        fname_omit,
    )


def test_an_existing_output_directory_is_used_as_is(tmp_path):
    """An output directory that exists already is used, whatever the input."""
    out = tmp_path / "out"
    out.mkdir()
    assert _out_dir(str(tmp_path / "dems"), out) == out


def test_with_no_output_directory_a_dem_directory_holds_the_summaries(tmp_path):
    """A directory of DEMs holds its own summaries."""
    assert _out_dir(str(tmp_path)) == tmp_path


def test_with_no_output_directory_a_dem_files_folder_holds_the_summaries(tmp_path):
    """A single DEM's summaries go in its file's folder."""
    assert _out_dir(str(tmp_path / "a.tif")) == tmp_path


def test_a_new_output_directory_is_taken_relative_to_a_dem_files_folder(tmp_path):
    """A relative output directory that doesn't exist yet goes in the DEM's folder."""
    assert _out_dir(str(tmp_path / "a.tif"), "new_out") == tmp_path / "new_out"


def test_a_list_uses_the_folder_of_its_first_dem_passing_the_filters(tmp_path):
    """The filters see file names only, not the folders above them."""
    dems = [
        str(tmp_path / "skip" / "a_omit.tif"),
        str(tmp_path / "skip" / "b.tif"),
        str(tmp_path / "keep_wgs84" / "c_wgs84.tif"),
    ]
    assert (
        _out_dir(dems, fname_filter="wgs84", fname_omit="omit")
        == tmp_path / "keep_wgs84"
    )
    assert _out_dir(dems, "out", fname_omit="omit") == tmp_path / "skip" / "out"


def test_summary_files_are_named_after_the_place(tmp_path):
    """Only the trailing '_results' is dropped, even if the place name has one."""
    files = validate_dem_collection._summary_files(tmp_path, "oregon results 2024")

    assert files.results_h5 == tmp_path / "oregon_results_2024_results.h5"
    assert files.stats == tmp_path / "oregon_results_2024_summary_stats.txt"
    assert files.plot == tmp_path / "oregon_results_2024_plot.png"
    assert files.csv == tmp_path / "oregon_results_2024_individual_results.csv"


def test_summary_files_without_a_place_are_named_summary(tmp_path):
    """With no place name, the summary files are named 'summary'."""
    files = validate_dem_collection._summary_files(tmp_path, None)

    assert files.results_h5 == tmp_path / "summary_results.h5"
    assert files.stats == tmp_path / "summary_summary_stats.txt"
