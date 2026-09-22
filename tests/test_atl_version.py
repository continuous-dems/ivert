"""Tests for the nsidc_atl_version plumbing.

Self-contained on purpose. The shared conftest and pytest configuration arrive
with the test scaffold (PR #102); this file needs neither, so it runs today
with ``python -m pytest tests/test_atl_version.py`` and simply falls under the
autouse isolation fixture once the scaffold lands. Nothing here touches the
developer's real ~/.ivert: every IS2Database and ICESat2RequestsCSV is built on
a stand-in config that points into tmp_path, and the network is stubbed out.
"""

import os
from types import SimpleNamespace
from typing import ClassVar

import pytest
from globato.streams.readers.icesat2 import ATL03Reader

from ivert import icesat2_database_v2 as is2db
from ivert.icesat2_requests import ICESat2RequestsCSV

V007_GRANULE = "ATL03_20241107234251_08052501_007_01_subsetted.h5"
V006_GRANULE = "ATL03_20241107234251_08052501_006_01_subsetted.h5"
BBOX = (-74.0, -73.0, 40.5, 41.0, 20230101, 20230201)


# ---------------------------------------------------------------------------
# Version normalisation and the release field of a granule filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "007"),
        ("7", "007"),
        ("007", "007"),
        (" 7 ", "007"),
        ("12", "012"),
    ],
)
def test_the_version_is_normalised_to_three_digits(value, expected):
    """Both the quoted config value and a hand-set int must match a filename's release field."""
    assert is2db._normalize_atl_version(value) == expected


@pytest.mark.parametrize("value", ["abc", "0007", "", "7.0", "-7", "7 1", None])
def test_a_value_that_is_not_a_version_is_rejected(value):
    with pytest.raises(ValueError, match="nsidc_atl_version"):
        is2db._normalize_atl_version(value)


def test_the_release_is_read_from_the_granule_filename():
    assert is2db._atl_release_from_filename(V007_GRANULE) == "007"
    # A directory prefix must not confuse the split on underscores.
    assert is2db._atl_release_from_filename(f"/some/dir_x/{V006_GRANULE}") == "006"


@pytest.mark.parametrize("name", ["granule.h5", "ATL03_20241107_0805.h5"])
def test_a_name_without_a_release_field_gives_none(name):
    assert is2db._atl_release_from_filename(name) is None


# ---------------------------------------------------------------------------
# The atl_version column of requests.csv
# ---------------------------------------------------------------------------


def _requests_csv(tmp_path):
    config = SimpleNamespace(icesat2_requests_csv=str(tmp_path / "requests.csv"))
    return ICESat2RequestsCSV(config=config)


def _harmony_job(job_id):
    return {
        "jobID": job_id,
        "createdAt": "2026-01-01T00:00:00Z",
        "dataExpiration": "2999-01-01T00:00:00Z",
        "numInputGranules": 3,
    }


def test_a_cached_job_is_found_only_for_the_version_it_asked_for(tmp_path):
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _harmony_job("job-007"), atl_version="007")

    assert (
        csv.find_matching_request("ATL03", BBOX, atl_version="007")["jobID"]
        == "job-007"
    )
    assert csv.find_matching_request("ATL03", BBOX, atl_version="006") is None
    # Asking for no version in particular still matches, as before the column.
    assert csv.find_matching_request("ATL03", BBOX)["jobID"] == "job-007"


def test_the_version_survives_a_round_trip_through_the_file_as_text(tmp_path):
    """Pandas would otherwise read "007" back as the integer 7, which no lookup matches."""
    _requests_csv(tmp_path).add_record(
        "ATL03",
        BBOX,
        _harmony_job("job-007"),
        atl_version="007",
    )

    reopened = _requests_csv(tmp_path)
    reopened.open()

    assert list(reopened.df["atl_version"]) == ["007"]
    assert (
        reopened.find_matching_request("ATL03", BBOX, atl_version="007")["jobID"]
        == "job-007"
    )


def test_a_file_from_before_the_column_existed_is_upgraded_on_read(tmp_path):
    """Old records carry no version, so a versioned lookup skips them and an unversioned one sees them.

    That means at most one job per region gets resubmitted after the upgrade.
    """
    job = _harmony_job("job-old")
    (tmp_path / "requests.csv").write_text(
        "atl_dataset,bbox,creation_date,expiration_date,job_id,json\n"
        f'ATL03,"{BBOX}",{job["createdAt"]},{job["dataExpiration"]},{job["jobID"]},"{job}"\n',
    )

    csv = _requests_csv(tmp_path)
    csv.open()

    assert list(csv.df.columns)[:2] == ["atl_dataset", "atl_version"]
    assert list(csv.df["atl_version"]) == [""]
    assert csv.find_matching_request("ATL03", BBOX, atl_version="007") is None
    assert csv.find_matching_request("ATL03", BBOX)["jobID"] == "job-old"


# ---------------------------------------------------------------------------
# The download loop: what is asked of fetchez, and what is done with the answer
# ---------------------------------------------------------------------------


class _FakeFetchezIceSat2:
    """Stands in for fetchez's IceSat2 module: records how it was built, does nothing."""

    built_with: ClassVar[list[dict]] = []

    def __init__(self, **kwargs: object) -> None:
        self.built_with.append(kwargs)
        self.subset_job_id = None
        self.results = []

    def harmony_ping_for_status(self, job_id):
        return None

    def harmony_make_request(self):
        return _harmony_job("job-new")

    def run(self):
        pass


class _FakeRequestsCSV:
    """Stands in for the requests cache: records the versions it is asked about."""

    lookups: ClassVar[list[dict]] = []
    records: ClassVar[list[dict]] = []

    def find_matching_request(self, *args: object, **kwargs: object):
        self.lookups.append(kwargs)

    def add_record(self, *args: object, **kwargs: object):
        self.records.append(kwargs)

    def update_record(self, *args: object, **kwargs: object):
        pass


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An IS2Database rooted in tmp_path, with the network replaced by the fakes."""
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        icesat2_download_directory=str(tmp_path / "cache"),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )
    _FakeFetchezIceSat2.built_with = []
    _FakeRequestsCSV.lookups = []
    _FakeRequestsCSV.records = []
    monkeypatch.setattr(is2db, "_FetchezIceSat2", _FakeFetchezIceSat2)
    monkeypatch.setattr(is2db, "ICESat2RequestsCSV", _FakeRequestsCSV)
    return is2db.IS2Database(ivert_config=config)


def _download(db, monkeypatch, granule_names):
    """Run download_new_granules over BBOX with fetchez "returning" these files.

    Returns the DownloadSummary and the granules that reached classification.
    """
    cache_dir = db.icesat2_download_dir
    os.makedirs(cache_dir, exist_ok=True)
    results = []
    for name in granule_names:
        path = os.path.join(cache_dir, name)
        with open(path, "wb"):
            pass
        results.append((None, {"status": 0, "dst_fn": path}))
    monkeypatch.setattr(is2db.fetchez.core, "run_fetchez", lambda _mods: results)

    processed = []

    def fake_classify(files_to_process, query_bbox, **kwargs: object):
        processed.extend(os.path.basename(h5_fn) for h5_fn, _ in files_to_process)
        return []

    monkeypatch.setattr(db, "_classify_files", fake_classify)

    return db.download_new_granules(BBOX), processed


def test_the_configured_version_is_requested_and_recorded(db, monkeypatch):
    _download(db, monkeypatch, [V007_GRANULE])

    assert [m["version"] for m in _FakeFetchezIceSat2.built_with] == ["007"]
    assert [k["atl_version"] for k in _FakeRequestsCSV.lookups] == ["007"]
    assert [k["atl_version"] for k in _FakeRequestsCSV.records] == ["007"]


def test_granules_of_another_release_are_left_out(db, monkeypatch):
    """The release of what came back is checked, not assumed.

    fetchez falls back to whatever Harmony serves for plain ATL03 when it does
    not know the version, so the request alone does not guarantee the release.
    """
    summary, processed = _download(db, monkeypatch, [V006_GRANULE, V007_GRANULE])

    assert processed == [V007_GRANULE]
    assert summary.parts_failed == 0


def test_a_part_with_only_wrong_release_granules_fails(db, monkeypatch):
    summary, processed = _download(db, monkeypatch, [V006_GRANULE])

    assert processed == []
    assert summary.parts_failed == 1
    assert summary.parts_attempted == 1


# ---------------------------------------------------------------------------
# What is handed to globato, and that the installed globato understands it
# ---------------------------------------------------------------------------


def test_globato_is_told_which_release_to_classify(db, tmp_path, monkeypatch):
    seen = {}

    def fake_read(*args: object, **kwargs: object):
        seen.update(kwargs)
        return iter([])

    monkeypatch.setattr(is2db.globato, "read", fake_read)
    db.config.nsidc_atl_version = 7  # an int, as a hand-edited config may give

    result = db._process_h5_to_nc(
        V007_GRANULE,
        str(tmp_path / "out.nc"),
        query_bbox=BBOX,
    )

    assert result is None
    assert seen["atl_version"] == "007"


def test_the_installed_globato_accepts_the_version_and_refuses_other_releases(tmp_path):
    """Compatibility with the globato actually installed, without any network."""
    granule = tmp_path / V007_GRANULE
    granule.write_bytes(b"")

    assert ATL03Reader(str(granule), atl_version="007").atl_version == "007"
    assert ATL03Reader(str(granule), atl_version=7).atl_version == "007"
    assert ATL03Reader(str(granule), atl_version=None).atl_version is None

    # The release check comes before the file is opened, so an empty file is enough.
    reader = ATL03Reader(str(granule), atl_version="006", use_external_masks=False)
    assert list(reader.yield_chunks()) == []
