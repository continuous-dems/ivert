"""The steps of IS2Database.download_new_granules around the fetch itself.

Which boxes are requested, which Harmony job a part uses, when a download is refused
for its datum, and which records a --replace download drops. Harmony, fetchez and
requests.csv are faked, so nothing here touches the network.
"""

import logging
from typing import ClassVar

import pytest
import shapely

from ivert import icesat2_database_v2 as is2db

DATES = (20230101, 20230201)
WEST = (0.0, 2.0, 0.0, 2.0, *DATES)
EAST = (2.0, 4.0, 0.0, 2.0, *DATES)


class _FakeFetchezIceSat2:
    """Stands in for fetchez's IceSat2: Harmony reports a cached job's status as set."""

    built: ClassVar[list[dict]] = []
    cached_job_status: ClassVar[str] = "successful"

    def __init__(self, **kwargs: object) -> None:
        self.built.append(kwargs)
        self.subset_job_id = None
        self.results = []
        self.requests_made = 0

    def harmony_ping_for_status(self, job_id: str) -> dict:
        return {"jobID": job_id, "status": self.cached_job_status}

    def harmony_make_request(self) -> dict:
        self.requests_made += 1
        return {"jobID": "job-new", "numInputGranules": 2}

    def run(self) -> None:
        pass


class _FakeRequestsCSV:
    """Stands in for requests.csv: holds the one cached job given, records additions."""

    cached: ClassVar[dict | None] = None
    added: ClassVar[list[tuple]] = []

    def __init__(self, config: object = None) -> None:
        pass

    def find_matching_request(self, *_args: object, **_kwargs: object) -> dict | None:
        return self.cached

    def add_record(self, *args: object, **_kwargs: object) -> None:
        self.added.append(args)

    def update_record(self, *_args: object, **_kwargs: object) -> None:
        pass


@pytest.fixture
def harmony(monkeypatch):
    """Fake Harmony, fetchez and requests.csv; fetchez downloads nothing."""
    _FakeFetchezIceSat2.built = []
    _FakeFetchezIceSat2.cached_job_status = "successful"
    _FakeRequestsCSV.cached = None
    _FakeRequestsCSV.added = []
    monkeypatch.setattr(is2db, "_FetchezIceSat2", _FakeFetchezIceSat2)
    monkeypatch.setattr(is2db, "ICESat2RequestsCSV", _FakeRequestsCSV)
    monkeypatch.setattr(is2db.fetchez.core, "run_fetchez", lambda _mods: [])
    return _FakeFetchezIceSat2


@pytest.fixture
def standin(make_standin_db):
    """A database holding one granule, over WEST."""
    standin = make_standin_db("db")
    standin.add_granule(WEST, [(0.5, 0.5), (1.5, 1.5)])
    standin.reindex()
    return standin


# ---------------------------------------------------------------------------
# The datum of the database
# ---------------------------------------------------------------------------
def test_a_download_in_another_datum_is_refused(make_standin_db, harmony, caplog):
    """A database must hold one datum, so nothing is requested, and it's a failure."""
    standin = make_standin_db("db", vertical_datum="geoid")
    standin.add_granule(WEST, [(0.5, 0.5)], vertical_datum="EPSG:4979")
    standin.reindex()

    with caplog.at_level(logging.ERROR):
        summary = standin.db.download_new_granules(EAST)

    assert summary == is2db.DownloadSummary(parts_failed=1)
    assert harmony.built == []
    assert "vertical datum EPSG:4979" in caplog.text
    assert "icesat2_vertical_datum='geoid'" in caplog.text


def test_a_download_in_the_same_datum_goes_ahead(standin):
    """The stand-in's ellipsoid config matches its EPSG:4979 granule."""
    assert not standin.db._datum_mismatch("EPSG:4979", "ellipsoid")


def test_an_empty_database_has_no_datum_to_conflict_with(make_standin_db):
    """Any datum may start a new database."""
    empty = make_standin_db("empty")

    assert not empty.db._datum_mismatch("EPSG:3855", "geoid")


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["", None, "EPSG:3855", "EPSG:4979"], "EPSG:3855"),
        (["", None], None),
        ([], None),
    ],
)
def test_the_first_value_set_is_found(values, expected):
    """Missing and blank values in an index column are passed over."""
    import pandas as pd  # noqa: PLC0415 - slow import

    assert is2db._first_set_value(pd.Series(values, dtype=object)) == expected


# ---------------------------------------------------------------------------
# The boxes requested
# ---------------------------------------------------------------------------
def test_an_area_already_in_the_database_requests_nothing(standin):
    """Coverage already stored isn't downloaded again."""
    assert standin.db._boxes_to_download(WEST, None, replace=False) == []


def test_replace_requests_the_whole_area_again(standin):
    """--replace downloads what is already stored, too."""
    boxes = standin.db._boxes_to_download(WEST, None, replace=True)

    assert [tuple(float(v) for v in b) for b in boxes] == [WEST]


def test_only_the_part_not_stored_is_requested(standin):
    """A request overlapping stored coverage asks only for the rest."""
    boxes = standin.db._boxes_to_download(
        (0.0, 4.0, 0.0, 2.0, *DATES),
        None,
        replace=False,
    )

    assert [tuple(float(v) for v in b) for b in boxes] == [EAST]


def test_an_area_with_no_extent_requests_nothing(standin):
    """An empty geometry has nothing to cover."""
    assert standin.db._boxes_to_download(EAST, shapely.Polygon(), replace=True) == []


# ---------------------------------------------------------------------------
# The Harmony job a part uses
# ---------------------------------------------------------------------------
def _attach(cached_status: str | None) -> tuple[_FakeFetchezIceSat2, bool]:
    if cached_status is not None:
        _FakeRequestsCSV.cached = {"jobID": "job-old", "numInputGranules": 3}
        _FakeFetchezIceSat2.cached_job_status = cached_status
    mod = _FakeFetchezIceSat2()
    attached = is2db.IS2Database._attach_harmony_job(
        mod,
        EAST,
        _FakeRequestsCSV(),
        "007",
    )
    return mod, attached


@pytest.mark.usefixtures("harmony")
@pytest.mark.parametrize("status", ["successful", "running"])
def test_a_cached_job_is_reused(status):
    """A job requests.csv holds, unless it ended badly, saves submitting another."""
    mod, attached = _attach(status)

    assert attached
    assert mod.subset_job_id == "job-old"
    assert mod.requests_made == 0
    assert _FakeRequestsCSV.added == []


@pytest.mark.usefixtures("harmony")
@pytest.mark.parametrize("status", sorted(is2db._HARMONY_ERROR_STATES))
def test_a_cached_job_that_ended_badly_is_replaced(status):
    """Its results can't be trusted, so a new job is submitted and recorded."""
    mod, attached = _attach(status)

    assert attached
    assert mod.subset_job_id == "job-new"
    assert mod.requests_made == 1
    assert len(_FakeRequestsCSV.added) == 1


@pytest.mark.usefixtures("harmony")
def test_with_no_cached_job_a_new_one_is_submitted_and_recorded():
    """The new job is written to requests.csv, for later runs to reuse."""
    mod, attached = _attach(None)

    assert attached
    assert mod.subset_job_id == "job-new"
    assert [(a[0], a[1]) for a in _FakeRequestsCSV.added] == [("ATL03", EAST)]


# ---------------------------------------------------------------------------
# What fetchez downloaded
# ---------------------------------------------------------------------------
def test_downloaded_files_are_those_fetched_and_on_disk(tmp_path):
    """Failed downloads are counted; a "successful" one with no file is left out."""
    b, a, gone = tmp_path / "b.h5", tmp_path / "a.h5", tmp_path / "gone.h5"
    b.touch()
    a.touch()
    results = [
        (None, {"status": 0, "dst_fn": str(b)}),
        (None, {"status": 0, "dst_fn": str(a)}),
        (None, {"status": 0, "dst_fn": str(gone)}),
        (None, {"status": -1, "dst_fn": str(tmp_path / "failed.h5")}),
    ]

    files, n_failed = is2db._downloaded_h5_files(results)

    assert files == [str(a), str(b)]
    assert n_failed == 1


# ---------------------------------------------------------------------------
# The records a --replace download drops
# ---------------------------------------------------------------------------
def test_replace_drops_the_same_granule_where_the_box_overlaps(standin):
    """The old record of a granule downloaded again over its box goes, file and all."""
    old = standin.db.open_gdf()
    old_file = standin.db.granules_dir / old["filename"].iloc[0]

    kept = standin.db._drop_replaced_records(old, old.copy(), WEST)

    assert len(kept) == 0
    assert not old_file.exists()


def test_replace_keeps_the_same_granule_over_other_dates(standin):
    """A record of the granule for another time range is other data, not a duplicate."""
    old = standin.db.open_gdf()

    kept = standin.db._drop_replaced_records(
        old,
        old.copy(),
        (0.0, 2.0, 0.0, 2.0, 20240101, 20240201),
    )

    assert len(kept) == 1
    assert (standin.db.granules_dir / old["filename"].iloc[0]).exists()


def test_replace_keeps_other_granules(standin):
    """Only granules downloaded again are replaced."""
    old = standin.db.open_gdf()
    new = old.assign(source_granule="ATL03_20990101000000_99990101_007_01")

    kept = standin.db._drop_replaced_records(old, new, WEST)

    assert len(kept) == 1
