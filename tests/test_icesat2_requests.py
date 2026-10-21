"""Tests for the Harmony request cache: matching, updating and cleaning its records.

Expiry dates and ATL versions have tests of their own, in test_harmony_expiry.py and
test_atl_version.py. The job records here have the fields, and the date format, of
the jobs Harmony returns.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from ivert.icesat2_requests import ICESat2RequestsCSV

BBOX = (-124.1, -124.0, 44.59, 44.64, 20200101, 20241101)
OTHER_BBOX = (-74.0, -73.0, 40.5, 41.0, 20230101, 20230201)
FUTURE = "2999-01-01T00:00:00.000Z"
PAST = "2020-01-01T00:00:00.000Z"


def _requests_csv(tmp_path) -> ICESat2RequestsCSV:
    config = SimpleNamespace(icesat2_requests_csv=str(tmp_path / "requests.csv"))
    return ICESat2RequestsCSV(config=config)


def _job(job_id: str, *, expires: str = FUTURE, status: str = "running") -> dict:
    """A Harmony job status, as Harmony returns it."""
    return {
        "createdAt": "2026-08-10T17:15:16.028Z",
        "dataExpiration": expires,
        "jobID": job_id,
        "links": [],
        "numInputGranules": 12,
        "progress": 100 if status == "successful" else 0,
        "status": status,
    }


def _reread(tmp_path) -> ICESat2RequestsCSV:
    """A fresh reader of the same file, so what's checked is what's on disk."""
    csv = _requests_csv(tmp_path)
    csv.open()
    return csv


# --- Matching -----------------------------------------------------------------------


def test_a_request_matches_within_the_tolerance(tmp_path):
    """Coordinates within the tolerance match; the dates must match exactly."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))

    nudged = (BBOX[0] + 1e-10, *BBOX[1:])
    assert csv.find_matching_request("ATL03", nudged)["jobID"] == "job-1"

    moved = (BBOX[0] + 1e-6, *BBOX[1:])
    assert csv.find_matching_request("ATL03", moved) is None
    assert csv.find_matching_request("ATL03", moved, tolerance=1e-5) is not None

    later = (*BBOX[:5], BBOX[5] + 1)
    assert csv.find_matching_request("ATL03", later, tolerance=10.0) is None


def test_a_request_matches_only_its_own_dataset(tmp_path):
    """The dataset name is matched case-insensitively, ignoring spaces."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))
    assert csv.find_matching_request(" atl03 ", BBOX)["jobID"] == "job-1"
    assert csv.find_matching_request("ATL08", BBOX) is None


def test_a_record_needs_a_full_box_with_dates(tmp_path):
    """A box without its date range can't be matched later, so it's refused."""
    with pytest.raises(ValueError, match="must have 6 values"):
        _requests_csv(tmp_path).add_record("ATL03", BBOX[:4], _job("job-1"))


# --- update_record ------------------------------------------------------------------


def test_an_update_replaces_the_jobs_status_on_disk(tmp_path):
    """The record of that job gets the new status; the others are left alone."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))
    csv.add_record("ATL03", BBOX, _job("job-2"))
    csv.add_record("ATL03", OTHER_BBOX, _job("job-3"))

    csv.update_record("ATL03", BBOX, _job("job-2", status="successful"))

    rows = _reread(tmp_path).find_matching_request("ATL03", BBOX, return_rows=True)
    statuses = {
        row["job_id"]: ICESat2RequestsCSV._read_json(row["json"])["status"]
        for _, row in rows.iterrows()
    }
    assert statuses == {"job-1": "running", "job-2": "successful"}
    other = _reread(tmp_path).find_matching_request("ATL03", OTHER_BBOX)
    assert other["status"] == "running"


def test_an_expired_job_can_still_be_updated(tmp_path):
    """Updating isn't reusing: an expired record's status is still replaced."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1", expires=PAST))
    csv.update_record("ATL03", BBOX, _job("job-1", expires=PAST, status="successful"))
    rows = _reread(tmp_path).find_matching_request(
        "ATL03",
        BBOX,
        only_unexpired=False,
        return_rows=True,
    )
    assert ICESat2RequestsCSV._read_json(rows.iloc[0]["json"])["status"] == "successful"


def test_an_update_without_writing_leaves_the_file_until_export(tmp_path):
    """With write_file=False the change is held until export()."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))
    csv.update_record(
        "ATL03",
        BBOX,
        _job("job-1", status="successful"),
        write_file=False,
    )
    assert _reread(tmp_path).find_matching_request("ATL03", BBOX)["status"] == "running"
    csv.export()
    assert (
        _reread(tmp_path).find_matching_request("ATL03", BBOX)["status"] == "successful"
    )


@pytest.mark.parametrize(
    ("bbox", "job_id", "match"),
    [
        (OTHER_BBOX, "job-1", "No matching record"),
        (BBOX, "job-9", "No record with jobID 'job-9'"),
    ],
    ids=["other box", "other job"],
)
def test_updating_a_job_that_isnt_recorded(tmp_path, bbox, job_id, match):
    """It's an error, or with fail_quietly, nothing happens and None comes back."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))
    before = (tmp_path / "requests.csv").read_text()

    with pytest.raises(ValueError, match=match):
        csv.update_record("ATL03", bbox, _job(job_id))
    assert csv.update_record("ATL03", bbox, _job(job_id), fail_quietly=True) is None
    assert (tmp_path / "requests.csv").read_text() == before


# --- clean_csv ----------------------------------------------------------------------


def test_cleaning_removes_expired_records_from_the_file(tmp_path):
    """Expired jobs are dropped from the file; unexpired ones stay."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-old", expires=PAST))
    csv.add_record("ATL03", BBOX, _job("job-new"))
    csv.add_record("ATL03", OTHER_BBOX, _job("job-other-old", expires=PAST))

    left = csv.clean_csv()

    assert list(left["job_id"]) == ["job-new"]
    assert list(_reread(tmp_path).open()["job_id"]) == ["job-new"]


def test_cleaning_with_nothing_expired_leaves_the_file_alone(tmp_path):
    """No expired records, no rewrite."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-1"))
    path = tmp_path / "requests.csv"
    before = path.stat().st_mtime_ns, path.read_text()
    assert list(csv.clean_csv()["job_id"]) == ["job-1"]
    assert (path.stat().st_mtime_ns, path.read_text()) == before


def test_a_lookup_can_clean_the_file_first(tmp_path):
    """auto_clean_csv drops expired records from the file before matching."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-old", expires=PAST))
    assert csv.find_matching_request("ATL03", BBOX, auto_clean_csv=True) is None
    assert _reread(tmp_path).open().empty


def test_a_job_can_be_updated_after_cleaning(tmp_path):
    """Cleaning leaves gaps in the row numbers; updating the next job still works."""
    csv = _requests_csv(tmp_path)
    csv.add_record("ATL03", BBOX, _job("job-old", expires=PAST))
    csv.add_record("ATL03", BBOX, _job("job-new"))
    csv.clean_csv()
    csv.update_record("ATL03", BBOX, _job("job-new", status="successful"))
    assert (
        _reread(tmp_path).find_matching_request("ATL03", BBOX)["status"] == "successful"
    )


# --- Reading the file ---------------------------------------------------------------


def test_a_missing_file_is_created_empty(tmp_path):
    """The first use writes an empty file with every column."""
    df = _requests_csv(tmp_path).open()
    assert df.empty
    written = pd.read_csv(tmp_path / "requests.csv")
    assert list(written.columns) == list(df.columns)
    assert "expiration_date" in written.columns


def test_a_missing_file_can_be_an_error_instead(tmp_path):
    """With create_if_nonexistent=False, a missing file isn't created."""
    with pytest.raises(FileNotFoundError):
        _requests_csv(tmp_path).open(create_if_nonexistent=False)
    assert not (tmp_path / "requests.csv").exists()


def test_a_file_being_written_is_read_again(tmp_path, monkeypatch):
    """Another process may be part-way through writing it, so a bad read is retried."""
    _requests_csv(tmp_path).add_record("ATL03", BBOX, _job("job-1"))
    real_read_csv = pd.read_csv
    attempts = []

    def flaky_read_csv(*args: object, **kwargs: object) -> pd.DataFrame:
        attempts.append(1)
        if len(attempts) < 3:
            msg = "Error tokenizing data"
            raise pd.errors.ParserError(msg)
        return real_read_csv(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", flaky_read_csv)
    assert _reread(tmp_path).find_matching_request("ATL03", BBOX)["jobID"] == "job-1"
    assert len(attempts) == 3


def test_a_file_that_never_reads_is_an_error(tmp_path, monkeypatch):
    """After repeated failures the read gives up and the error is raised."""
    _requests_csv(tmp_path).add_record("ATL03", BBOX, _job("job-1"))

    def broken_read_csv(*_args: object, **_kwargs: object) -> pd.DataFrame:
        msg = "Error tokenizing data"
        raise pd.errors.ParserError(msg)

    monkeypatch.setattr(pd, "read_csv", broken_read_csv)
    with pytest.raises(pd.errors.ParserError):
        _requests_csv(tmp_path).open()


def test_a_record_whose_job_isnt_a_dictionary_is_an_error(tmp_path):
    """A hand-edited record that no longer holds a job dictionary isn't reused."""
    (tmp_path / "requests.csv").write_text(
        "atl_dataset,atl_version,bbox,creation_date,expiration_date,job_id,json\n"
        f'ATL03,007,"{BBOX}",2026-08-10,{FUTURE},job-1,"[1, 2]"\n',
    )
    with pytest.raises(TypeError, match="not a dictionary"):
        _reread(tmp_path).find_matching_request("ATL03", BBOX)


def test_a_version_field_that_isnt_a_version_matches_nothing(tmp_path):
    """A hand-edited version is kept as written, so the job isn't reused for one."""
    (tmp_path / "requests.csv").write_text(
        "atl_dataset,atl_version,bbox,creation_date,expiration_date,job_id,json\n"
        f'ATL03,latest,"{BBOX}",2026-08-10,{FUTURE},job-1,"{_job("job-1")}"\n',
    )
    csv = _reread(tmp_path)
    assert list(csv.open()["atl_version"]) == ["latest"]
    assert csv.find_matching_request("ATL03", BBOX, atl_version="007") is None
    assert csv.find_matching_request("ATL03", BBOX)["jobID"] == "job-1"
