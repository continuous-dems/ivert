"""How the photon-curtain plotter reads its options and pairs .nc and .h5 photons."""

import pandas as pd
import pytest

from ivert import plot_photon_clouds_v2 as ppc


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
def test_no_vdatum_leaves_the_plots_in_egm2008():
    """Without -V/--vdatum nothing is transformed, and the axis says EGM2008."""
    target_vert, ylabel = ppc._resolve_target_vdatum(None)

    assert target_vert is None
    assert "EGM2008" in ylabel


def test_an_unknown_vdatum_stops_the_plotter():
    """The message names the datum given."""
    with pytest.raises(SystemExit, match="Unknown vertical datum: 'bogus'"):
        ppc._resolve_target_vdatum("bogus")


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [(None, None, None), (0.0, None, (0.0, None)), (None, 5.0, (None, 5.0))],
)
def test_limits_are_none_unless_an_end_is_set(low, high, expected):
    """One end alone still limits the axis; the other stays open."""
    assert ppc._limits(low, high) == expected


@pytest.mark.parametrize(
    ("classes", "expected"),
    [(None, None), ("", set()), ("1/40/41", {1, 40, 41})],
)
def test_classes_none_means_all_and_empty_means_none(classes, expected):
    """'' shows every photon as noise, unlike leaving --classes out."""
    assert ppc._parse_classes(classes) == expected


# ---------------------------------------------------------------------------
# Which .h5 file is plotted
# ---------------------------------------------------------------------------
GID = "ATL03_20230101000000_01010101_007_01"


@pytest.fixture
def files(tmp_path):
    """An .nc granule beside the .h5 it came from, and another .h5 elsewhere."""
    nc = tmp_path / f"{GID}_subsetted_W080.00000_W079.00000_N25.00000_N26.00000.nc"
    h5 = tmp_path / f"{GID}_subsetted.h5"
    other = tmp_path / "elsewhere" / "other.h5"
    other.parent.mkdir()
    for path in (nc, h5, other):
        path.touch()
    return nc, h5, other


def test_an_h5_input_is_plotted_on_its_own(files):
    """Given an .h5 file, that is the file plotted, whatever --h5 says."""
    _, h5, other = files

    assert ppc._h5_only_path(h5, str(other), []) == h5


def test_h5_only_uses_the_h5_named(files):
    """--h5-only with --h5 PATH plots that file."""
    nc, _, other = files

    assert ppc._h5_only_path(nc, str(other), []) == other


def test_h5_only_finds_the_granules_h5(files):
    """--h5-only without a path finds the .h5 of the .nc file's granule."""
    nc, h5, _ = files

    assert ppc._h5_only_path(nc, None, []) == h5


def test_h5_only_stops_when_no_h5_is_found(tmp_path):
    """With nothing to plot, the plotter says so."""
    nc = tmp_path / f"{GID}_subsetted.nc"
    nc.touch()

    with pytest.raises(SystemExit, match=r"no matching \.h5 found"):
        ppc._h5_only_path(nc, None, [])


def test_h5_only_stops_when_the_h5_named_is_missing(files):
    """A mistyped --h5 path is an error, not a search for another file."""
    nc, _, _ = files

    with pytest.raises(SystemExit, match=r"\.h5 file not found"):
        ppc._h5_only_path(nc, str(nc.parent / "missing.h5"), [])


def test_a_background_h5_named_but_missing_stops_the_plotter(files):
    """A mistyped --h5 path is an error, not a plot without a background."""
    nc, _, _ = files

    with pytest.raises(SystemExit, match=r"\.h5 file not found"):
        ppc._background_h5_path(nc, str(nc.parent / "missing.h5"), [], search=False)


def test_without_h5_there_is_no_background(files):
    """Leaving --h5 out plots the .nc photons alone."""
    nc, _, _ = files

    assert ppc._background_h5_path(nc, None, [], search=False) is None


def test_h5_without_a_path_finds_the_background(files):
    """--h5 alone finds the .h5 of the .nc file's granule."""
    nc, h5, _ = files

    assert ppc._background_h5_path(nc, None, [], search=True) == h5


def test_an_h5_not_found_leaves_no_background_and_warns(tmp_path, caplog):
    """Not finding the background is a warning; the .nc photons are still plotted."""
    nc = tmp_path / f"{GID}_subsetted.nc"
    nc.touch()

    assert ppc._background_h5_path(nc, None, [], search=True) is None
    assert "no matching .h5 found" in caplog.text


# ---------------------------------------------------------------------------
# Matching .nc photons to an .h5 beam
# ---------------------------------------------------------------------------
def _bg():
    """Three .h5 photons of one beam, with their along-track distances."""
    return pd.DataFrame(
        {
            "delta_time": [1.0, 2.0, 3.0],
            "x": [10.0, 11.0, 12.0],
            "y": [20.0, 21.0, 22.0],
            "along_track_m": [0.0, 5.0, 10.0],
        },
    )


def test_a_beams_photons_are_picked_by_laser():
    """The laser column says which beam each .nc photon is from."""
    nc = pd.DataFrame(
        {"delta_time": [1.0, 2.0], "x": [10.0, 11.0], "y": [20.0, 21.0]},
    ).assign(laser=["gt1l", "gt2r"])

    beam = ppc._nc_beam_photons(nc, _bg(), "gt1l")

    assert list(beam["delta_time"]) == [1.0]


def test_without_laser_a_beams_photons_are_matched_to_the_h5():
    """An old .nc file's photons are matched to the beam's by time and place."""
    nc = pd.DataFrame(
        {
            "delta_time": [1.0, 2.0, 9.0],
            "x": [10.0, 11.0, 19.0],
            "y": [20.0, 21.0, 29.0],
        },
    )

    beam = ppc._nc_beam_photons(nc, _bg(), "gt1l")

    assert list(beam["delta_time"]) == [1.0, 2.0]


def test_missing_along_track_distances_come_from_the_h5():
    """Photons with no match in the .h5 have no distance, so are dropped."""
    beam = pd.DataFrame(
        {"delta_time": [2.0, 9.0], "x": [11.0, 19.0], "y": [21.0, 29.0]},
    )

    filled = ppc._with_along_track(beam, _bg())

    assert list(filled["along_track_m"]) == [5.0]


def test_along_track_distances_already_there_are_kept():
    """A current .nc file's own distances are used as they are."""
    beam = pd.DataFrame(
        {"delta_time": [9.0], "x": [19.0], "y": [29.0], "along_track_m": [42.0]},
    )

    assert ppc._with_along_track(beam, _bg()) is beam
