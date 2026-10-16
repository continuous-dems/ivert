"""Tests for how a collection validation picks DEMs from a directory or file list."""

from pathlib import Path

from ivert.validate_dem_collection import _resolve_dem_list


def _touch(directory, *names: str):
    for name in names:
        (directory / name).write_bytes(b"")
    return [str(directory / name) for name in names]


def test_directory_keeps_raster_formats_and_skips_sidecars(tmp_path):
    """Collections once kept only *.tif; other GDAL formats count now.

    Sidecar files and subdirectories still don't.
    """
    _touch(
        tmp_path,
        "a.tif",
        "b.TIFF",
        "c.grd",
        "d.vrt",
        "e.img",
        "a.tif.aux.xml",
        "e.img.ovr",
        "notes.txt",
    )
    (tmp_path / "ivert_results").mkdir()

    dems = _resolve_dem_list(str(tmp_path), None, None)

    assert [p.rsplit("/", 1)[1] for p in dems] == [
        "a.tif",
        "b.TIFF",
        "c.grd",
        "d.vrt",
        "e.img",
    ]


def test_explicit_non_tif_files_are_kept(tmp_path):
    """The old .tif filter applied to named files too.

    So 'ivert validate a.nc b.nc' validated nothing.
    """
    files = _touch(tmp_path, "a.flt", "b.asc")

    assert _resolve_dem_list(files, None, None) == files


def test_explicit_list_drops_sidecars_with_a_warning(tmp_path, caplog):
    """A glob such as *.tif* picks up sidecars, which are dropped.

    They are named, since the user listed them.
    """
    files = _touch(tmp_path, "a.tif", "a.tif.aux.xml", "b.tif")

    dems = _resolve_dem_list(files, None, None)

    assert dems == [files[0], files[2]]
    assert "a.tif.aux.xml" in caplog.text


def test_fname_filter_and_omit_still_apply(tmp_path):
    """The user's own name filter and omit pattern still work beside the format list."""
    _touch(tmp_path, "x_wgs84.tif", "x_navd88.tif", "y_wgs84.nc")

    dems = _resolve_dem_list(str(tmp_path), r"_wgs84", r"\.nc\Z")

    assert [p.rsplit("/", 1)[1] for p in dems] == ["x_wgs84.tif"]


def test_a_single_dem_given_as_a_path(tmp_path):
    """A single DEM given as a Path raised TypeError; it comes back as a str."""
    (dem,) = _touch(tmp_path, "a.tif")

    assert _resolve_dem_list(tmp_path / "a.tif", None, None) == [dem]


def test_an_empty_list_gives_no_dems():
    """An empty list raised TypeError from Path([]); it is just no DEMs."""
    assert _resolve_dem_list([], None, None) == []


def test_a_list_of_paths(tmp_path, caplog):
    """A list of Paths raised TypeError when a sidecar was named or a filter was given.

    The names come back as strs.
    """
    files = _touch(tmp_path, "x_wgs84.tif", "x_wgs84.tif.aux.xml", "y_navd88.tif")

    dems = _resolve_dem_list([Path(f) for f in files], r"_wgs84", None)

    assert dems == [files[0]]
    assert "x_wgs84.tif.aux.xml" in caplog.text


def test_ivert_outputs_beside_the_dems_are_not_dems(tmp_path):
    """A rerun into the DEMs' folder validated the error rasters (#214).

    Each run then added another layer, such as a_errors_errors.tif, and the error
    rasters' results went into the collection's summary.
    """
    _touch(
        tmp_path,
        "a.tif",
        "a_errors.tif",
        "a_errors.gpkg",
        "a_results.h5",
        "a_photons.h5",
    )

    dems = _resolve_dem_list(str(tmp_path), None, None)

    assert [Path(p).name for p in dems] == ["a.tif"]
