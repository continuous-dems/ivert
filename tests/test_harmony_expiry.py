"""Tests for when a recorded Harmony job has expired and must not be reused."""

from types import SimpleNamespace

import pytest

from ivert.icesat2_requests import ICESat2RequestsCSV

BBOX = (-74.0, -73.0, 40.5, 41.0, 20230101, 20230201)


def _requests_csv(tmp_path):
    config = SimpleNamespace(icesat2_requests_csv=str(tmp_path / "requests.csv"))
    return ICESat2RequestsCSV(config=config)


@pytest.mark.parametrize(
    ("expiration", "expired"),
    [
        ("2020-01-01T00:00:00Z", True),
        ("2999-01-01T00:00:00Z", False),
        # No time zone: taken as UTC. These counted as never expiring.
        ("2020-01-01T00:00:00", True),
        ("2020-01-01", True),
        ("2999-01-01T00:00:00", False),
        # Blank (NaN once read back from the CSV) or unreadable: expired, since
        # Harmony deletes every job's results in time. A blank one crashed every
        # lookup, and an unreadable one was reused forever.
        ("", True),
        (float("nan"), True),
        ("not a date", True),
    ],
)
def test_expiration_dates(expiration, expired):
    """A date is expired once it has passed, whether or not it gives a time zone."""
    assert ICESat2RequestsCSV._is_expired(expiration) is expired


def test_an_expired_job_without_a_time_zone_is_not_reused(tmp_path):
    """Reusing it would download from a job whose results Harmony has deleted."""
    (tmp_path / "requests.csv").write_text(
        "atl_dataset,atl_version,bbox,creation_date,expiration_date,job_id,json\n"
        f'ATL03,007,"{BBOX}",2019-12-01T00:00:00,2020-01-01T00:00:00,job-old,'
        "\"{'jobID': 'job-old'}\"\n",
    )
    csv = _requests_csv(tmp_path)
    csv.open()

    assert csv.find_matching_request("ATL03", BBOX, atl_version="007") is None


def test_a_job_recorded_without_an_expiration_date_is_not_reused(tmp_path):
    """Harmony gave no 'dataExpiration', so add_record() wrote a blank.

    Read back, the blank is NaN, which dateparser refused with a TypeError on every
    later lookup. A new job is submitted instead.
    """
    _requests_csv(tmp_path).add_record(
        "ATL03",
        BBOX,
        {"jobID": "job-1", "createdAt": "2026-01-01T00:00:00Z"},
        atl_version="007",
    )
    csv = _requests_csv(tmp_path)
    csv.open()

    assert csv.find_matching_request("ATL03", BBOX, atl_version="007") is None
