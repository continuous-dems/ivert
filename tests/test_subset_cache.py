"""Each part of a download must fetch and read its own Harmony subsets.

Harmony names every subset of a granule alike whatever box it was cut to, and
fetchez keeps any file that already exists. Adjacent parts of a request share
every granule whose ground track crosses their common edge, so under Harmony's
name the second part would read the first part's subset, clip it to its own
box, and lose those granules' photons. The subsets are therefore renamed with
the query suffix before anything is fetched.

The database is built on a stand-in config in tmp_path and fetchez is faked, so
nothing here touches the developer's real ~/.ivert or the network.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest
import shapely
import xarray

from ivert import icesat2_database_v2 as is2db

GRANULE = "ATL03_20211102061743_06231306_007_01_subsetted.h5"
STEM = "ATL03_20211102061743_06231306_007_01_subsetted"
PART_1 = (-121.0, -116.5, 32.0, 33.0, 20211101, 20241101)
PART_2 = (-122.0, -116.5, 33.0, 34.0, 20211101, 20241101)
SUFFIX_1 = "_W121.00000_W116.50000_N32.00000_N33.00000_20211101_20241101"


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def test_a_subset_is_named_after_its_granule_and_its_box():
    """Harmony names a granule's subsets alike whatever their box; this name keeps them apart."""
    assert (
        is2db.IS2Database._subset_cache_filename(GRANULE, PART_1)
        == STEM + SUFFIX_1 + ".h5"
    )
    # A directory prefix is dropped; the name is what goes in the cache directory.
    assert (
        is2db.IS2Database._subset_cache_filename(f"/cache/{GRANULE}", PART_1)
        == STEM + SUFFIX_1 + ".h5"
    )


def test_two_boxes_give_the_same_granule_two_names():
    """Under one name, the second part of a request read the first part's subset."""
    names = {
        is2db.IS2Database._subset_cache_filename(GRANULE, b) for b in (PART_1, PART_2)
    }
    assert len(names) == 2


def test_the_nc_name_does_not_repeat_a_suffix_already_on_the_h5_name():
    """The .nc name carries one suffix whether the h5 was named by Harmony or by us."""
    plain = is2db.IS2Database._nc_filename(GRANULE, PART_1)
    renamed = is2db.IS2Database._nc_filename(STEM + SUFFIX_1 + ".h5", PART_1)

    assert plain == renamed == STEM + SUFFIX_1 + ".nc"


def test_the_granule_id_fields_stay_in_front_of_the_suffix():
    """Globato and the release check read fields 1-3 of the name split on '_'."""
    renamed = is2db.IS2Database._subset_cache_filename(GRANULE, PART_1)

    assert renamed.split("_")[:5] == GRANULE.split("_")[:5]


def test_the_source_granule_is_still_recovered_from_the_nc_name():
    """Code that maps a tile back to its granule reads the .nc name, so the suffix must strip off."""
    nc = is2db.IS2Database._nc_filename(STEM + SUFFIX_1 + ".h5", PART_1)

    assert is2db.IS2Database._source_granule_from_filename(nc) == STEM


# ---------------------------------------------------------------------------
# The download loop: one request per rectangle, one .nc per storage tile
# ---------------------------------------------------------------------------


class _FakeFetchezIceSat2:
    """Stands in for fetchez's IceSat2 module: "polls" to one granule under Harmony's name."""

    built: ClassVar[list] = []

    def __init__(self, **kwargs: object) -> None:
        self.__class__.built.append(kwargs)
        self.outdir = kwargs["outdir"]
        self.subset_job_id = None
        self.results = []

    def harmony_ping_for_status(self, _job_id):
        return None

    def harmony_make_request(self):
        return {"jobID": "job-1", "numInputGranules": 1}

    def run(self):
        self.results = [
            {
                "url": f"https://harmony/{GRANULE}",
                # fetchez's result entries hold strings.
                "dst_fn": str(Path(self.outdir) / GRANULE),
            },
        ]


class _FakeRequestsCSV:
    def __init__(self, config: object = None) -> None:
        pass

    def find_matching_request(self, *_args: object, **_kwargs: object):
        return None

    def add_record(self, *_args: object, **_kwargs: object):
        pass

    def update_record(self, *_args: object, **_kwargs: object):
        pass


def _fake_run_fetchez(mods):
    """Create each result file where the module says to, as fetchez would."""
    out = []
    for mod in mods:
        for entry in mod.results:
            Path(entry["dst_fn"]).touch()
            out.append((mod, {**entry, "status": 0}))
    return out


def _photons(xs, y=32.5):
    """Return a classified-photon table like _classify_h5 returns, one photon per x."""
    t0 = is2db._yyyymmdd_to_delta_time(20230101) + 1.0
    return pd.DataFrame(
        {
            "x": list(xs),
            "y": [y] * len(xs),
            "z": [1.0] * len(xs),
            "class_code": [1] * len(xs),
            "delta_time": [t0 + i for i in range(len(xs))],
            "confidence": [4] * len(xs),
            # 4-byte beam names, as globato hands them over and the .nc files store them.
            "laser": [b"gt1l"] * len(xs),
        },
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An IS2Database rooted in tmp_path, with fetchez, the requests cache and the prefetch faked."""
    config = SimpleNamespace(
        ivert_database_index=str(tmp_path / "db" / "_ivert_database_index.nc"),
        ivert_database_directory=str(tmp_path / "db"),
        ivert_landmask_directory=str(tmp_path / "landmasks"),
        icesat2_download_directory=str(tmp_path / "cache"),
        icesat2_vertical_datum="ellipsoid",
        nsidc_atl_version="007",
    )
    _FakeFetchezIceSat2.built = []
    monkeypatch.setattr(is2db, "_FetchezIceSat2", _FakeFetchezIceSat2)
    monkeypatch.setattr(is2db, "ICESat2RequestsCSV", _FakeRequestsCSV)
    monkeypatch.setattr(is2db.fetchez.core, "run_fetchez", _fake_run_fetchez)
    # The ATL08/ATL24 prefetch would look these made-up granules up at NSIDC.
    monkeypatch.setattr(
        is2db.IS2Database,
        "_start_aux_prefetch",
        lambda _self, _files: None,
    )
    return is2db.IS2Database(ivert_config=config)


def _region(kwargs):
    r = kwargs["src_region"]
    return (r.w, r.e, r.s, r.n)


def test_a_plain_box_is_requested_whole(db, monkeypatch):
    """No more 2-degree splitting of the request: a 5x5 box is one Harmony job."""
    monkeypatch.setattr(db, "_classify_h5", lambda *_a, **_k: None)

    db.download_new_granules((0.0, 5.0, 0.0, 5.0, 20230101, 20230201))

    assert [_region(b) for b in _FakeFetchezIceSat2.built] == [(0.0, 5.0, 0.0, 5.0)]


def test_a_plain_box_and_the_same_box_as_a_geometry_make_the_same_request(
    db,
    monkeypatch,
):
    """A box and a geometry share one path, so a rectangle is one Harmony request either way.

    One large subset of a granule costs Harmony less than several small ones.
    """
    monkeypatch.setattr(db, "_classify_h5", lambda *_a, **_k: None)
    box = (-121.0, -116.5, 32.0, 33.0, 20230101, 20230201)

    db.download_new_granules(box)
    as_plain = [_region(b) for b in _FakeFetchezIceSat2.built]
    _FakeFetchezIceSat2.built = []
    db.download_new_granules(box, geometry=shapely.box(-121.0, 32.0, -116.5, 33.0))
    as_geometry = [_region(b) for b in _FakeFetchezIceSat2.built]

    assert as_plain == as_geometry == [(-121.0, -116.5, 32.0, 33.0)]


def test_a_subset_is_classified_once_and_stored_per_tile(db, monkeypatch):
    """A 4.5-degree rectangle is one download but two storage tiles."""
    calls = []

    def fake_classify(h5_fn, query_bbox, **_kwargs: object):
        calls.append((Path(h5_fn).name, query_bbox))
        return _photons([-120.5, -118.0, -117.0]), "EPSG:4979"

    monkeypatch.setattr(db, "_classify_h5", fake_classify)

    summary = db.download_new_granules(PART_1)

    assert calls == [(STEM + SUFFIX_1 + ".h5", PART_1)]
    tiles = is2db.split_bbox_into_parts(PART_1)
    assert [t[:4] for t in tiles] == [
        (-121.0, -119.0, 32.0, 33.0),
        (-119.0, -116.5, 32.0, 33.0),
    ]
    written = sorted(path.name for path in db.granules_dir.iterdir())
    assert written == sorted(
        [is2db.IS2Database._nc_filename(GRANULE, t) for t in tiles]
        + ["_ivert_database_index.nc"],
    )
    counts = {}
    for name in written:
        if name.startswith("ATL03"):
            ds = xarray.open_dataset(db.granules_dir / name)
            counts[tuple(ds.attrs["query_bbox"][:4])] = int(ds.attrs["numphotons"])
            ds.close()
    assert counts == {(-121.0, -119.0, 32.0, 33.0): 1, (-119.0, -116.5, 32.0, 33.0): 2}
    assert summary.granules_added == 2


def test_a_tile_with_no_photons_gets_no_file(db, monkeypatch):
    """Tiles a granule crosses without photons would otherwise fill the database with empty files."""
    monkeypatch.setattr(
        db,
        "_classify_h5",
        lambda *_a, **_k: (_photons([-120.5]), "EPSG:4979"),
    )

    db.download_new_granules(PART_1)

    nc_files = [p.name for p in db.granules_dir.iterdir() if p.name.startswith("ATL03")]
    assert nc_files == [
        is2db.IS2Database._nc_filename(GRANULE, is2db.split_bbox_into_parts(PART_1)[0]),
    ]


def test_low_confidence_bathy_floor_photons_are_not_stored(db, monkeypatch):
    """-bc/--bathy-confidence drops class-40 photons below it before they are written."""
    photons = _photons([-120.5, -120.4, -120.3, -120.2]).assign(
        class_code=[40, 40, 40, 1],
        bathy_confidence=[0.2, 0.5, 0.9, 0.1],
    )
    monkeypatch.setattr(db, "_classify_h5", lambda *_a, **_k: (photons, "EPSG:4979"))

    db.download_new_granules(PART_1, min_bathy_confidence=0.5)

    (name,) = [p.name for p in db.granules_dir.iterdir() if p.name.startswith("ATL03")]
    with xarray.open_dataset(db.granules_dir / name) as ds:
        kept = ds[["class_code", "bathy_confidence"]].to_dataframe()
    # The class-40 photon below 0.5 is gone; the land photon stays whatever its value.
    assert sorted(zip(kept["class_code"], kept["bathy_confidence"], strict=True)) == [
        (1, 0.1),
        (40, 0.5),
        (40, 0.9),
    ]


def test_each_part_fetches_and_reads_its_own_copy_of_a_shared_granule(db, monkeypatch):
    """The case in the module docstring: adjacent parts share a granule, and each reads its own subset."""
    read = []

    def fake_classify(h5_fn, _query_bbox, **_kwargs: object):
        read.append(Path(h5_fn).name)

    monkeypatch.setattr(db, "_classify_h5", fake_classify)

    for part in (PART_1, PART_2):
        db.download_new_granules(part)

    assert len(set(read)) == 2, read
    assert all(name.startswith(STEM + "_W") for name in read)
    assert sorted(p.name for p in db.icesat2_download_dir.iterdir()) == sorted(read)
