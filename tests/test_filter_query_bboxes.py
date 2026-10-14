"""Tests for the boxes IS2Database.filter_query_bboxes() hands back."""

from types import SimpleNamespace

import pytest

from ivert import icesat2_database_v2 as is2db

QUERY = (-121.0, -119.0, 32.0, 34.0, 20211101, 20241101)
# Already in the database: the western half of QUERY, over the same dates.
EXISTING = (-121.0, -120.0, 32.0, 34.0, 20211101, 20241101)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An IS2Database whose index already covers EXISTING."""
    config = SimpleNamespace(
        ivert_database_directory=str(tmp_path / "db"),
        ivert_landmask_directory=str(tmp_path / "landmasks"),
        icesat2_download_directory=str(tmp_path / "cache"),
    )
    database = is2db.IS2Database(ivert_config=config)
    monkeypatch.setattr(
        database,
        "unique_bboxes",
        lambda **_kwargs: [EXISTING],
    )
    return database


def test_the_dates_of_a_remaining_box_are_integers(db):
    """merge_cuboids() made the dates floats, so requests.csv got 20211101.0."""
    remaining = db.filter_query_bboxes([QUERY])

    assert remaining == [(-120.0, -119.0, 32.0, 34.0, 20211101, 20241101)]
    assert all(type(box[4]) is int and type(box[5]) is int for box in remaining)
