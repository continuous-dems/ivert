"""Which granules and photons 'ivert database convert' reads, and when it asks first."""

from pathlib import Path

import click
import pytest

from ivert import cli
from ivert import icesat2_database_v2 as is2db

WEST = (0.0, 2.0, 0.0, 2.0, 20230101, 20230201)
EAST = (2.0, 4.0, 0.0, 2.0, 20230601, 20230701)


def _target(bbox=None, geometry=None) -> cli._ExportTarget:
    kind = "all" if bbox is None and geometry is None else "region"
    return cli._ExportTarget(kind=kind, bbox=bbox, geometry=geometry, path=None)


@pytest.fixture
def standin(make_standin_db):
    """Two granules side by side, a few months apart: one of class 1, one of 40."""
    standin = make_standin_db("db")
    standin.add_granule(WEST, [(0.5, 0.5), (1.5, 1.5)], date=20230101)
    standin.add_granule(
        EAST,
        [(2.5, 0.5), (3.5, 1.5)],
        date=20230601,
        class_code=40,
    )
    standin.reindex()
    return standin


# ---------------------------------------------------------------------------
# Choosing the granules
# ---------------------------------------------------------------------------
def test_with_no_region_or_dates_every_granule_is_read(standin):
    """Nothing to filter by gives every granule in the database."""
    rows = cli._granules_to_convert(standin.db, None, None)

    assert len(rows) == 2


def test_empty_tile_markers_are_not_read(standin):
    """A marker for a tile with no photons is no granule to read."""
    standin.db._write_empty_tile((4.0, 6.0, 0.0, 2.0, 20230101, 20230201), "EPSG:4979")
    standin.reindex()

    rows = cli._granules_to_convert(standin.db, None, None)

    assert len(rows) == 2
    assert not standin.db.empty_tile_rows(rows).any()


def test_a_region_picks_the_granules_inside_it(standin):
    """Only the granule under the bbox is read."""
    rows = cli._granules_to_convert(standin.db, (0.2, 1.8, 0.2, 1.8), None)

    assert len(rows) == 1
    assert rows["query_bbox_xmax"].iloc[0] == 2.0


def test_dates_alone_pick_the_granules_in_that_time(standin):
    """A date range with no bbox searches the whole globe."""
    rows = cli._granules_to_convert(standin.db, None, (20230501, 20230801))

    assert len(rows) == 1
    assert rows["query_bbox_xmin"].iloc[0] == 2.0


def test_an_empty_database_is_an_error(make_standin_db):
    """With nothing to read, the user is told how to fill the database."""
    empty = make_standin_db("empty")
    empty.reindex()

    with pytest.raises(click.ClickException, match="ivert database download"):
        cli._granules_to_convert(empty.db, None, None)


def test_no_granule_in_the_region_is_an_error(standin):
    """A region with no granules says so, rather than writing an empty file."""
    with pytest.raises(click.ClickException, match="No granules found"):
        cli._granules_to_convert(standin.db, (10.0, 11.0, 10.0, 11.0), None)


# ---------------------------------------------------------------------------
# Reading the photons
# ---------------------------------------------------------------------------
def _read(standin, target, *, class_list=None, delta_time_range=None):
    rows = cli._granules_to_convert(standin.db, target.bbox, None)
    return cli._read_granule_photons(
        standin.db,
        rows,
        target,
        class_list=class_list,
        delta_time_range=delta_time_range,
    )


def test_every_photon_of_the_granules_is_read(standin):
    """With no filters, all four photons come back, in WGS84."""
    gdf = _read(standin, _target())

    assert len(gdf) == 4
    assert gdf.crs.to_epsg() == 4326


def test_photons_outside_the_region_are_dropped(standin):
    """The granules are cut to the bbox, not just chosen by it."""
    gdf = _read(standin, _target(bbox=(0.0, 1.0, 0.0, 1.0)))

    assert list(gdf["x"]) == [0.5]


def test_photons_outside_the_polygons_are_dropped(standin):
    """A polygon region cuts photons to the polygon, within its bbox."""
    import shapely  # noqa: PLC0415 - slow import

    gdf = _read(
        standin,
        _target(bbox=(0.0, 4.0, 0.0, 2.0), geometry=shapely.box(3.0, 1.0, 4.0, 2.0)),
    )

    assert list(gdf["x"]) == [3.5]


def test_only_the_classes_asked_for_are_read(standin):
    """A class filter keeps only those classes' photons."""
    gdf = _read(standin, _target(), class_list=[40])

    assert set(gdf["class_code"]) == {40}
    assert len(gdf) == 2


def test_only_photons_in_the_time_range_are_read(standin):
    """A delta_time range keeps only the photons inside it."""
    start = is2db._yyyymmdd_to_delta_time(20230501)
    end = is2db._yyyymmdd_to_delta_time(20230801)

    gdf = _read(standin, _target(), delta_time_range=(start, end))

    assert sorted(gdf["x"]) == [2.5, 3.5]


def test_a_granule_file_missing_from_disk_is_skipped(standin, capsys):
    """A file the index lists but the disk lacks is skipped, and the user told."""
    rows = cli._granules_to_convert(standin.db, None, None)
    missing = rows["filename"].iloc[0]
    (Path(standin.db.granules_dir) / missing).unlink()

    gdf = cli._read_granule_photons(
        standin.db,
        rows,
        _target(),
        class_list=None,
        delta_time_range=None,
    )

    assert len(gdf) == 2
    assert f"Skipping missing granule file: {missing}" in capsys.readouterr().err


def test_no_photon_left_after_filtering_is_an_error(standin):
    """Granules whose photons are all filtered out give an error, not an empty file."""
    with pytest.raises(click.ClickException, match="No photons found"):
        _read(standin, _target(), class_list=[41])


# ---------------------------------------------------------------------------
# Asking before a large conversion
# ---------------------------------------------------------------------------
def _must_not_ask(*_args: object, **_kwargs: object) -> bool:
    raise AssertionError


@pytest.fixture
def large(standin, monkeypatch):
    """The standin's index rows, with the large-export threshold lowered to them."""
    monkeypatch.setattr(cli, "_EXPORT_WARN_PHOTON_THRESHOLD", 4)
    return cli._granules_to_convert(standin.db, None, None)


def test_a_large_conversion_asks_and_stops_if_told_to(large, monkeypatch, capsys):
    """Saying no aborts the conversion; the warning names the scope and size."""
    monkeypatch.setattr(click, "confirm", lambda *_a, **_k: False)

    with pytest.raises(click.Abort):
        cli._confirm_large_convert(large, whole_database=True, force=False)

    err = capsys.readouterr().err
    assert "the entire database" in err
    assert "2 granule(s)" in err


def test_a_large_conversion_goes_on_if_told_to(large, monkeypatch):
    """Saying yes carries on."""
    monkeypatch.setattr(click, "confirm", lambda *_a, **_k: True)

    cli._confirm_large_convert(large, whole_database=False, force=False)


def test_a_forced_large_conversion_does_not_ask(large, monkeypatch):
    """-f/--force skips the question."""
    monkeypatch.setattr(click, "confirm", _must_not_ask)

    cli._confirm_large_convert(large, whole_database=True, force=True)


def test_a_small_conversion_does_not_ask(standin, monkeypatch):
    """Below the threshold there is nothing to confirm."""
    monkeypatch.setattr(click, "confirm", _must_not_ask)
    rows = cli._granules_to_convert(standin.db, None, None)

    cli._confirm_large_convert(rows, whole_database=True, force=False)
