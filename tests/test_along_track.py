# tests/test_along_track.py
"""Each classified photon gets its cumulative along-track distance from the .h5."""

import os
from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd
import pytest

from ivert import icesat2_database_v2 as is2db

# Two beams. Segment photon counts include an empty segment, whose
# ph_index_beg is 0 in a real file.
BEAMS = {
    "gt1l": {
        "segment_id": [100, 101, 102, 103],
        "cnt": [2, 0, 3, 1],
        "length": [20.0, 20.0, 19.0, 21.0],
    },
    "gt3r": {"segment_id": [200, 201], "cnt": [1, 2], "length": [20.0, 20.0]},
}


def _write_h5(path):
    with h5py.File(path, "w") as f:
        for beam, spec in BEAMS.items():
            cnt = np.array(spec["cnt"], dtype=np.int32)
            n = int(cnt.sum())
            starts = np.concatenate(([0], np.cumsum(cnt)[:-1]))
            g = f.create_group(f"{beam}/geolocation")
            g["segment_id"] = np.array(spec["segment_id"], dtype=np.int32)
            g["segment_ph_cnt"] = cnt
            g["segment_length"] = np.array(spec["length"])
            g["ph_index_beg"] = np.where(cnt > 0, starts + 1, 0).astype(np.int32)
            h = f.create_group(f"{beam}/heights")
            # Within a segment photons sit 1, 2, 3 ... metres along.
            h["dist_ph_along"] = np.concatenate(
                [np.arange(1, c + 1, dtype=float) for c in cnt],
            )
            h["delta_time"] = 1e8 + np.arange(n) * 1e-4
            h["lon_ph"] = -80.0 + np.arange(n) * 1e-5
            h["lat_ph"] = 25.0 + np.arange(n) * 1e-5
            h["h_ph"] = np.full(n, 10.0, dtype=np.float32)
            geo = f.create_group(f"{beam}/geophys_corr")
            geo["delta_time"] = np.array([1e8, 1e8 + 1.0])
            geo["geoid"] = np.array([-25.0, -25.0], dtype=np.float32)
    return str(path)


def _expected(beam, segment_id, idx):
    spec = BEAMS[beam]
    i = spec["segment_id"].index(segment_id)
    return sum(spec["length"][:i]) + idx


def test_tables_cover_every_beam_and_photon(tmp_path):
    h5 = _write_h5(tmp_path / "ATL03_test.h5")

    tables = is2db.IS2Database._h5_along_track_m(h5, ["gt1l", "gt3r", "gt2l"])

    assert set(tables) == {"gt1l", "gt3r"}  # gt2l is not in the file
    _segment_id, seg_starts, along = tables["gt1l"]
    assert seg_starts.tolist() == [0, 2, 2, 5]
    # Segment 102 starts 40 m along; its 3 photons sit 1, 2, 3 m into it.
    assert along.tolist() == [1.0, 2.0, 41.0, 42.0, 43.0, 60.0]


def test_photons_are_looked_up_by_beam_segment_and_place(tmp_path):
    h5 = _write_h5(tmp_path / "ATL03_test.h5")
    tables = is2db.IS2Database._h5_along_track_m(h5, list(BEAMS))
    df = pd.DataFrame(
        {
            # globato yields the beam as bytes; the 3rd photon has coordinates
            # that no longer match the file, as an ATL24 bathymetry photon does.
            "laser": [b"gt1l", b"gt3r", b"gt1l", b"gt1l", b"gt1l"],
            "ph_segment_id": [102, 201, 100, 999, 103],
            "ph_index_within_seg": [3, 2, 2, 1, 1],
            "x": [0.0, 0.0, 12345.0, 0.0, 0.0],
        },
    )

    along = is2db.IS2Database._along_track_of_photons(df, tables)

    assert along[0] == _expected("gt1l", 102, 3)
    assert along[1] == _expected("gt3r", 201, 2)
    assert along[2] == _expected("gt1l", 100, 2)
    assert np.isnan(along[3])  # a segment the file does not have
    assert along[4] == _expected("gt1l", 103, 1)


def test_beam_names_may_be_str(tmp_path):
    h5 = _write_h5(tmp_path / "ATL03_test.h5")
    tables = is2db.IS2Database._h5_along_track_m(h5, list(BEAMS))
    df = pd.DataFrame(
        {"laser": ["gt3r"], "ph_segment_id": [200], "ph_index_within_seg": [1]},
    )
    assert is2db.IS2Database._along_track_of_photons(df, tables).tolist() == [1.0]


def _records(h5_fn, delta_time):
    """What globato yields for a granule: 3 kept photons on two beams."""
    return np.rec.fromarrays(
        [
            np.array([-80.0, -80.1, -80.2]),
            np.array([25.0, 25.1, 25.2]),
            np.array([1.0, 2.0, -3.0]),
            np.array([b"gt1l", b"gt1l", b"gt3r"], dtype="S4"),
            np.array([4, 4, 3], dtype=np.int16),
            np.array([delta_time] * 3),
            np.array([1, 1, 40]),
            np.array([-1.0, -1.0, 0.8]),
            np.array([100, 102, 201], dtype=np.int32),
            np.array([2, 3, 2], dtype=np.int64),
        ],
        names="x,y,z,laser,confidence,delta_time,ph_h_classed,bathy_confidence,ph_segment_id,ph_index_within_seg",
    )


def test_classified_photons_carry_along_track_m(tmp_path, monkeypatch):
    h5 = _write_h5(tmp_path / "ATL03_test.h5")
    query_bbox = (-81.0, -79.0, 24.0, 26.0, 20220101, 20220201)
    delta_time = is2db._yyyymmdd_to_delta_time(20220115)
    monkeypatch.setattr(
        is2db.globato,
        "read",
        lambda *_a, **_k: iter([_records(h5, delta_time)]),
    )
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        icesat2_download_directory=str(tmp_path),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )
    db = is2db.IS2Database(ivert_config=config)

    df, _ = db._classify_h5(h5, query_bbox, classes_to_keep=(1, 40))

    assert "along_track_m" in df.columns
    assert df["along_track_m"].tolist() == [
        _expected("gt1l", 100, 2),
        _expected("gt1l", 102, 3),
        _expected("gt3r", 201, 2),
    ]
    assert "ph_segment_id" not in df.columns  # only the stored columns remain

    # And it reaches the tile.
    nc_fn = str(tmp_path / "db" / "tile.nc")
    db._write_nc(df, h5, nc_fn, query_bbox, "EPSG:4979")
    import xarray

    with xarray.open_dataset(nc_fn) as tile:
        assert tile["along_track_m"].to_numpy().tolist() == df["along_track_m"].tolist()


def test_no_along_track_without_the_segment_columns(tmp_path, monkeypatch):
    h5 = _write_h5(tmp_path / "ATL03_test.h5")
    query_bbox = (-81.0, -79.0, 24.0, 26.0, 20220101, 20220201)
    delta_time = is2db._yyyymmdd_to_delta_time(20220115)
    records = _records(h5, delta_time)
    without = records[[n for n in records.dtype.names if n != "ph_index_within_seg"]]
    monkeypatch.setattr(is2db.globato, "read", lambda *_a, **_k: iter([without]))
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        icesat2_download_directory=str(tmp_path),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )

    df, _ = is2db.IS2Database(ivert_config=config)._classify_h5(h5, query_bbox)

    assert "along_track_m" not in df.columns
    assert len(df) == 3


@pytest.mark.parametrize("name", ["gt1l", "gt3r"])
def test_the_plotter_computes_the_same_distances_from_the_h5(tmp_path, name):
    """The plotter's own .h5 computation agrees with the tile's.

    plot_photon_clouds_v2 recomputes the column from the .h5 when it has one; the
    two must agree, including across an empty segment and at each segment's first
    photon (the plotter used to search a 0-based row against 1-based ph_index_beg).
    """
    from ivert.plot_photon_clouds_v2 import _load_h5_beam_photons

    h5 = _write_h5(tmp_path / "ATL03_test.h5")

    plotter = _load_h5_beam_photons(h5, name).sort_values("delta_time")
    _, _, along = is2db.IS2Database._h5_along_track_m(h5, [name])[name]

    assert plotter["along_track_m"].tolist() == along.tolist()
    assert os.path.exists(h5)
