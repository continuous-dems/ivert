"""Tests for how the download loop hands Harmony jobs to fetchez.

IVERT's requests.csv decides which Harmony job each part uses; fetchez must
neither pick a job of its own nor answer from a cache of another job's links.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from ivert import icesat2_database_v2 as is2db

BBOX = (-74.0, -73.0, 40.5, 41.0, 20230101, 20230201)


class _FakeFetchezIceSat2:
    """Stands in for fetchez's IceSat2 module; Harmony refuses or accepts as told."""

    built_with: ClassVar[list[dict]] = []
    runs: ClassVar[list[object]] = []
    accept = True

    def __init__(self, **kwargs: object) -> None:
        self.built_with.append(kwargs)
        self.subset_job_id = None
        self.results = []

    def harmony_ping_for_status(self, _job_id):
        return None

    def harmony_make_request(self):
        if not self.accept:
            return None
        return {"jobID": "job-new", "status": "running", "links": []}

    def run(self):
        self.runs.append(self.subset_job_id)


class _FakeRequestsCSV:
    """Stands in for requests.csv: holds no jobs, records what is added."""

    records: ClassVar[list[tuple]] = []

    def __init__(self, config: object = None) -> None:
        pass

    def find_matching_request(self, *_args: object, **_kwargs: object):
        return None

    def add_record(self, *args: object, **_kwargs: object):
        self.records.append(args)

    def update_record(self, *_args: object, **_kwargs: object):
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An IS2Database rooted in tmp_path, with Harmony and requests.csv faked."""
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        ivert_landmask_directory=str(tmp_path / "db" / "landmasks"),
        icesat2_download_directory=str(tmp_path / "cache"),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )
    Path(config.icesat2_download_directory).mkdir(parents=True)
    _FakeFetchezIceSat2.built_with = []
    _FakeFetchezIceSat2.runs = []
    _FakeFetchezIceSat2.accept = True
    _FakeRequestsCSV.records = []
    monkeypatch.setattr(is2db, "_FetchezIceSat2", _FakeFetchezIceSat2)
    monkeypatch.setattr(is2db, "ICESat2RequestsCSV", _FakeRequestsCSV)
    monkeypatch.setattr(is2db.fetchez.core, "run_fetchez", lambda _mods: [])
    return is2db.IS2Database(ivert_config=config)


def test_a_refused_submission_fails_the_part(db):
    """Without a job ID, fetchez's run() would submit a job requests.csv never records."""
    _FakeFetchezIceSat2.accept = False

    summary = db.download_new_granules(BBOX)

    assert summary.parts_failed == 1
    assert _FakeFetchezIceSat2.runs == []
    assert _FakeRequestsCSV.records == []


def test_fetchez_does_not_answer_from_its_cache(db):
    """The fetchez cache keys links without the job ID, so a new job got an old job's links."""
    db.download_new_granules(BBOX)

    assert [m.get("use_cache") for m in _FakeFetchezIceSat2.built_with] == [False]
    assert _FakeFetchezIceSat2.runs == ["job-new"]
