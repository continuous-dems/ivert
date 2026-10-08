"""Tests for how the photon-cloud plotter finds the ATL03 .h5 behind a granule .nc file."""

from types import SimpleNamespace

from ivert import plot_photon_clouds_v2 as ppc

GRANULE = "ATL03_20240307011821_12102202_007_01"
NC_NAME = f"{GRANULE}_subsetted_W121.00000_W119.00000_N34.00000_N35.00000_20211101_20241101.nc"


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_the_h5_is_found_in_a_cache_moved_by_the_config(tmp_path):
    """A cache outside ~/.ivert used to be missed: only the hard-coded ~/.ivert paths were searched."""
    nc = _touch(tmp_path / "database" / NC_NAME)
    config = SimpleNamespace(
        icesat2_download_directory=str(tmp_path / "bigdisk" / "icesat2"),
        cache_directory=str(tmp_path / "bigdisk"),
    )
    h5 = _touch(tmp_path / "bigdisk" / "icesat2" / "subdir" / f"{GRANULE}_subsetted.h5")

    assert ppc._find_h5(nc, ppc._h5_search_dirs(config)) == h5


def test_an_h5_beside_the_nc_file_is_preferred(tmp_path):
    """The .nc file's own folder is searched before the cache."""
    nc = _touch(tmp_path / "work" / NC_NAME)
    beside = _touch(tmp_path / "work" / f"{GRANULE}.h5")
    _touch(tmp_path / "cache" / f"{GRANULE}.h5")

    assert ppc._find_h5(nc, [tmp_path / "cache"]) == beside


def test_no_match_gives_none(tmp_path):
    """Another granule's .h5, or a cache folder that doesn't exist, is no match and no error."""
    nc = _touch(tmp_path / "work" / NC_NAME)
    _touch(tmp_path / "cache" / "ATL03_20990101000000_00000000_007_01.h5")

    assert ppc._find_h5(nc, [tmp_path / "cache", tmp_path / "missing"]) is None


def test_a_folder_named_by_both_settings_is_searched_once(tmp_path):
    """By default the download folder is the cache folder."""
    config = SimpleNamespace(
        icesat2_download_directory=str(tmp_path / "cache"),
        cache_directory=str(tmp_path / "cache"),
    )

    assert ppc._h5_search_dirs(config) == [tmp_path / "cache"]
