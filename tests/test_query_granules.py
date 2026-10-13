"""Tests for IS2Database.query_granules()' date bounds."""

from types import SimpleNamespace

import pandas as pd
import pytest

from ivert import icesat2_database_v2 as is2db

# A granule whose photons all fall on one day, as nearly every granule's do.
GRANULE_DAY = 20211102
BOX = (-121.0, -116.5, 32.0, 33.0)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An IS2Database whose index holds the one granule."""
    index = pd.DataFrame(
        {
            "data_bbox_xmin": [-120.0],
            "data_bbox_xmax": [-119.0],
            "data_bbox_ymin": [32.5],
            "data_bbox_ymax": [32.6],
            "data_bbox_tmin": [GRANULE_DAY],
            "data_bbox_tmax": [GRANULE_DAY],
        },
    )
    config = SimpleNamespace(
        ivert_database_directory=str(tmp_path / "db"),
        ivert_landmask_directory=str(tmp_path / "landmasks"),
        icesat2_download_directory=str(tmp_path / "cache"),
    )
    database = is2db.IS2Database(ivert_config=config)
    monkeypatch.setattr(database, "open_gdf", lambda: index)
    return database


@pytest.mark.parametrize(
    ("start", "end", "found"),
    [
        (20211101, 20211110, True),
        # '--start-date' on the granule's own day dropped it: the index's days are
        # inclusive, but the overlap test was strict.
        (20211102, 20211110, True),
        (20211103, 20211110, False),
        # The end date is exclusive, so a granule on the end day is left out.
        (20211025, 20211102, False),
        (20211025, 20211103, True),
    ],
)
def test_the_start_day_is_included_and_the_end_day_is_not(db, start, end, found):
    """A granule counts from its first day through its last, start day included."""
    result = db.query_granules((*BOX, start, end))

    assert (len(result) == 1) is found
