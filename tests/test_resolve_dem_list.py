"""Tests for how a collection validation picks its DEMs out of a directory or file list."""

from ivert.validate_dem_collection import _resolve_dem_list


def _touch(directory, *names: str):
    for name in names:
        (directory / name).write_bytes(b"")
    return [str(directory / name) for name in names]


def test_directory_keeps_raster_formats_and_skips_sidecars(tmp_path):
    _touch(
        tmp_path,
        "a.tif",
        "b.TIFF",
        "c.nc",
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
        "c.nc",
        "d.vrt",
        "e.img",
    ]


def test_explicit_non_tif_files_are_kept(tmp_path):
    files = _touch(tmp_path, "a.nc", "b.asc")

    assert _resolve_dem_list(files, None, None) == files


def test_explicit_list_drops_sidecars_with_a_warning(tmp_path, caplog):
    files = _touch(tmp_path, "a.tif", "a.tif.aux.xml", "b.tif")

    dems = _resolve_dem_list(files, None, None)

    assert dems == [files[0], files[2]]
    assert "a.tif.aux.xml" in caplog.text


def test_fname_filter_and_omit_still_apply(tmp_path):
    _touch(tmp_path, "x_wgs84.tif", "x_navd88.tif", "y_wgs84.nc")

    dems = _resolve_dem_list(str(tmp_path), r"_wgs84", r"\.nc\Z")

    assert [p.rsplit("/", 1)[1] for p in dems] == ["x_wgs84.tif"]
