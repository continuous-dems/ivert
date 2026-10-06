"""Tests that failures surface as failures rather than as empty or partial results."""

import os
from multiprocessing import shared_memory
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ivert import icesat2_database_v2 as is2db
from ivert import validate_dem


@pytest.fixture
def db(tmp_path):
    """An IS2Database rooted in tmp_path."""
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        ivert_landmask_directory=str(tmp_path / "db" / "landmasks"),
        icesat2_download_directory=str(tmp_path / "cache"),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )
    return is2db.IS2Database(ivert_config=config)


@pytest.mark.parametrize("empty_index", [None, pd.DataFrame()])
def test_querying_an_empty_index_finds_no_photons(db, monkeypatch, empty_index):
    """An index with no granules, as a rebuild of an empty database writes, is no error."""
    monkeypatch.setattr(db, "open_gdf", lambda: empty_index)

    assert db.query_photons((-74.0, 40.5, -73.0, 41.0, 20230101, 20230201)) is None


def test_a_crash_in_cell_validation_is_raised_not_returned(monkeypatch):
    """A failure partway through must not hand back the cells finished so far.

    The caller writes whatever comes back as the DEM's complete results, and a
    later run without --overwrite would then reuse them.
    """

    def crash(*_args: object, **_kwargs: object):
        msg = "child failed to start"
        raise RuntimeError(msg)

    monkeypatch.setattr(validate_dem, "kick_off_new_child_process", crash)

    n = 3
    photon_df = pd.DataFrame(
        {
            "i": np.arange(n, dtype=np.int64),
            "j": np.zeros(n, dtype=np.int64),
            "class_code": np.ones(n, dtype=np.int8),
            "h": np.ones(n, dtype=np.float32),
        },
    )

    with pytest.raises(RuntimeError, match="child failed to start"):
        validate_dem._run_parallel_cell_validation(
            photon_df,
            photon_df["h"],
            dem_overlap_i=photon_df["i"].to_numpy(),
            dem_overlap_j=photon_df["j"].to_numpy(),
            dem_overlap_elevs=np.zeros(n, dtype=np.float32),
            n=n,
            max_photons_per_cell=None,
            min_photons_per_cell=1,
            measure_coverage=False,
            coverage_coords=None,
            numprocs=1,
            empty_val=-99999.0,
        )

    # The shared memory segments are released on the way out.
    with pytest.raises(FileNotFoundError):
        shared_memory.SharedMemory(name=f"heights_{os.getpid()}")
