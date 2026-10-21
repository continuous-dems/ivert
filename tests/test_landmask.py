"""Tests for IVERT's local store of OpenStreetMap landmasks.

fetchez.get("osm_landmask", ...) is replaced with a stand-in that writes what the
real module writes (checked against real fetches): a GeoJSON FeatureCollection at
<outdir>/osm_landmask/osm_landmask_<W>_<S>_<E>_<N>_e_binary.geojson, its land as
polygons with {"class": "land"}, an empty FeatureCollection for open water, and a
lock file beside it. In the stand-in's world, everything west of lon 0.5 is land.
"""

import json
import logging
from pathlib import Path

import pytest
import shapely

from ivert import landmask

COAST_LON = 0.5
WORLD_LAND = shapely.box(-180.0, -90.0, COAST_LON, 90.0)
# The shoreline stops this far short of a rectangle's edge, which is not shoreline.
EDGE_GAP = 1e-5


def _osm_file(cache_dir: Path, bbox, *, subdir: str = "osm_landmask") -> Path:
    """Write the OSM landmask fetchez caches for an (xmin, xmax, ymin, ymax) box."""
    w, e, s, n = (float(v) for v in bbox)
    folder = cache_dir / subdir
    folder.mkdir(parents=True, exist_ok=True)
    land = WORLD_LAND.intersection(shapely.box(w, s, e, n))
    features = [
        {
            "type": "Feature",
            "properties": {"class": "land"},
            "geometry": shapely.geometry.mapping(part),
        }
        for part in shapely.get_parts(land)
        if not part.is_empty
    ]
    (folder / f"temp_osm_{w}_{s}_{e}_{n}_e_binary.json.lock").touch()
    path = folder / f"osm_landmask_{w}_{s}_{e}_{n}_e_binary.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    return path


@pytest.fixture
def dirs(tmp_path):
    """(store_dir, cache_dir) for one test."""
    return tmp_path / "landmasks", tmp_path / "cache"


@pytest.fixture
def fetches(monkeypatch):
    """Replace fetchez.get with the stand-in; return the regions it was asked for.

    Assign fetches.fail to make every fetch raise, or fetches.write_nothing to make
    it return without writing a file.
    """

    class Fetches(list):
        fail = False
        write_nothing = False

    calls = Fetches()

    def fake_get(module, *, region, outdir, verbose):
        assert module == "osm_landmask"
        assert isinstance(outdir, str)
        assert verbose is False
        calls.append(list(region))
        if calls.fail:
            msg = "OSM is down"
            raise ConnectionError(msg)
        if not calls.write_nothing:
            _osm_file(Path(outdir), region)

    monkeypatch.setattr(landmask.fetchez, "get", fake_get)
    return calls


def _land_west_of_coast(bbox):
    xmin, xmax, ymin, ymax = bbox
    return shapely.box(xmin, ymin, min(xmax, COAST_LON), ymax)


# --- The store's files --------------------------------------------------------------


def test_stored_landmasks_are_listed_by_their_rectangle(dirs):
    """A file named by store_filename() is found again with its rectangle."""
    store, _ = dirs
    bbox = (-1.5, 2.0, 40.0, 41.25)
    landmask.write_land(landmask.store_filename(store, bbox), shapely.Polygon())
    (store / "landmask_notes.geojson").write_text("{}")
    (store / "other.geojson").write_text("{}")
    assert landmask.stored_landmasks(store) == {
        landmask.store_filename(store, bbox): bbox,
    }


def test_written_land_reads_back_the_same(dirs):
    """Land written to the store reads back unchanged, inside a given box."""
    store, _ = dirs
    land = shapely.union(shapely.box(0, 0, 1, 1), shapely.box(2, 0, 3, 1))
    path = store / "land.geojson"
    landmask.write_land(path, land)
    back = landmask.land_from_files([path], (0.0, 3.0, 0.0, 1.0))
    assert back.symmetric_difference(land).area == pytest.approx(0.0)


def test_only_polygons_are_written(dirs):
    """Stray lines or points left by a clip are not land, and are dropped."""
    store, _ = dirs
    mixed = shapely.GeometryCollection(
        [shapely.box(0, 0, 1, 1), shapely.LineString([(2, 0), (3, 1)])],
    )
    path = store / "land.geojson"
    landmask.write_land(path, mixed)
    features = json.loads(path.read_text())["features"]
    assert [f["geometry"]["type"] for f in features] == ["Polygon"]


def test_open_water_is_an_empty_file(dirs):
    """A rectangle with no land is stored as a file with no features."""
    store, _ = dirs
    path = store / "water.geojson"
    landmask.write_land(path, shapely.Polygon())
    assert json.loads(path.read_text())["features"] == []
    assert landmask.land_from_files([path], (0.0, 1.0, 0.0, 1.0)).is_empty


def test_a_failed_write_leaves_no_file(dirs, monkeypatch):
    """A write that fails part-way leaves neither the file nor its temporary copy."""
    store, _ = dirs

    def broken_dump(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(landmask.json, "dump", broken_dump)
    with pytest.raises(OSError, match="disk full"):
        landmask.write_land(store / "land.geojson", shapely.box(0, 0, 1, 1))
    assert list(store.iterdir()) == []


def test_land_from_several_files_is_merged_and_clipped(dirs):
    """Two files' land is joined into one shape and cut to the box asked for."""
    store, _ = dirs
    left, right = store / "left.geojson", store / "right.geojson"
    landmask.write_land(left, shapely.box(0, 0, 1, 1))
    landmask.write_land(right, shapely.box(1, 0, 2, 1))
    land = landmask.land_from_files([left, right], (0.5, 1.5, 0.0, 1.0))
    assert land.symmetric_difference(shapely.box(0.5, 0, 1.5, 1)).area == pytest.approx(
        0.0,
    )


def test_clipping_a_stored_landmask_makes_a_new_one(dirs):
    """The clipped part is stored under its own rectangle's name."""
    store, _ = dirs
    big = (0.0, 2.0, 0.0, 2.0)
    landmask.write_land(landmask.store_filename(store, big), shapely.box(0, 0, 1, 2))
    small = (0.5, 1.5, 0.5, 1.5)
    out = landmask.clip_stored_landmask(
        landmask.store_filename(store, big),
        small,
        store,
    )
    assert out == landmask.store_filename(store, small)
    land = landmask.land_from_files([out], small)
    assert land.symmetric_difference(shapely.box(0.5, 0.5, 1, 1.5)).area == (
        pytest.approx(0.0)
    )


# --- Filling the store --------------------------------------------------------------


def test_a_rectangle_the_cache_covers_is_filled_without_fetching(dirs, fetches):
    """A cached OSM landmask over the rectangle is used; OSM isn't asked."""
    store, cache = dirs
    _osm_file(cache, (-1.0, 2.0, 0.0, 2.0))
    bbox = (0.0, 1.0, 0.0, 1.0)
    assert landmask.ensure_landmasks([bbox], store, cache) == [bbox]
    assert fetches == []
    land = landmask.land_from_files([landmask.store_filename(store, bbox)], bbox)
    assert land.symmetric_difference(_land_west_of_coast(bbox)).area == (
        pytest.approx(0.0)
    )


def test_several_cached_landmasks_can_cover_a_rectangle_together(dirs, fetches):
    """Two cached files that only between them cover the rectangle are enough."""
    store, cache = dirs
    _osm_file(cache, (0.0, 0.5, 0.0, 1.0))
    _osm_file(cache, (0.5, 1.0, 0.0, 1.0), subdir="icesat2/osm_landmask")
    assert landmask.ensure_landmasks([(0.0, 1.0, 0.0, 1.0)], store, cache) == [
        (0.0, 1.0, 0.0, 1.0),
    ]
    assert fetches == []


def test_a_rectangle_the_cache_only_partly_covers_is_fetched(dirs, fetches):
    """Cached land over part of the rectangle isn't enough: OSM is asked for it all."""
    store, cache = dirs
    _osm_file(cache, (0.0, 0.5, 0.0, 1.0))
    bbox = (0.0, 1.0, 0.0, 1.0)
    assert landmask.ensure_landmasks([bbox], store, cache) == [bbox]
    assert fetches == [[0.0, 1.0, 0.0, 1.0]]
    assert landmask.store_filename(store, bbox).exists()


def test_rectangles_already_stored_are_skipped(dirs, fetches):
    """A stored rectangle isn't filled again, and isn't reported as added."""
    store, cache = dirs
    bbox = (0.0, 1.0, 0.0, 1.0)
    landmask.ensure_landmasks([bbox], store, cache)
    assert landmask.ensure_landmasks([bbox, bbox], store, cache) == []
    assert len(fetches) == 1


def test_values_past_the_rectangle_are_ignored(dirs, fetches):
    """A database tile's date range doesn't change which rectangle is stored."""
    store, cache = dirs
    added = landmask.ensure_landmasks(
        [(0, 1, 0, 1, 20230101, 20230201)],
        store,
        cache,
    )
    assert added == [(0.0, 1.0, 0.0, 1.0)]
    assert fetches == [[0.0, 1.0, 0.0, 1.0]]


def test_without_fetching_an_uncached_rectangle_is_left_out(dirs, fetches):
    """With fetch=False, only what the cache covers is stored."""
    store, cache = dirs
    _osm_file(cache, (0.0, 1.0, 0.0, 1.0))
    added = landmask.ensure_landmasks(
        [(0.0, 1.0, 0.0, 1.0), (1.0, 2.0, 0.0, 1.0)],
        store,
        cache,
        fetch=False,
    )
    assert added == [(0.0, 1.0, 0.0, 1.0)]
    assert fetches == []


def test_a_failed_fetch_is_logged_and_the_rest_carry_on(dirs, fetches, caplog):
    """A rectangle OSM can't give is left out with a warning; others still fill."""
    store, cache = dirs
    _osm_file(cache, (1.0, 2.0, 0.0, 1.0))
    fetches.fail = True
    with caplog.at_level(logging.WARNING, logger=landmask.__name__):
        added = landmask.ensure_landmasks(
            [(0.0, 1.0, 0.0, 1.0), (1.0, 2.0, 0.0, 1.0)],
            store,
            cache,
        )
    assert added == [(1.0, 2.0, 0.0, 1.0)]
    assert "Could not fetch the OSM landmask for 0/1/0/1" in caplog.text


def test_a_fetch_that_brings_back_nothing_is_logged(dirs, fetches, caplog):
    """A fetch that returns without a file leaves the rectangle out, with a warning."""
    store, cache = dirs
    fetches.write_nothing = True
    with caplog.at_level(logging.WARNING, logger=landmask.__name__):
        added = landmask.ensure_landmasks([(0.0, 1.0, 0.0, 1.0)], store, cache)
    assert added == []
    assert "No OSM landmask came back for 0/1/0/1" in caplog.text


# --- Loading a landmask for a DEM ---------------------------------------------------


def test_a_landmask_comes_from_the_store_when_it_covers_the_box(dirs, fetches):
    """Nothing is fetched; the land, its coverage and the shoreline come back."""
    store, cache = dirs
    landmask.ensure_landmasks([(0.0, 1.0, 0.0, 1.0)], store, cache)
    fetches.clear()

    bbox = (0.25, 0.75, 0.25, 0.75)
    land, covered, coastline = landmask.load_landmask(bbox, str(store), str(cache))
    assert fetches == []
    assert land.symmetric_difference(_land_west_of_coast(bbox)).area == (
        pytest.approx(0.0)
    )
    assert covered.area == pytest.approx(0.25)
    assert coastline.length == pytest.approx(0.5, abs=EDGE_GAP)
    assert coastline.bounds == pytest.approx(
        (COAST_LON, 0.25, COAST_LON, 0.75),
        abs=EDGE_GAP,
    )


@pytest.mark.usefixtures("fetches")
def test_rectangle_edges_are_not_shoreline(dirs):
    """Where land meets the edge of a stored rectangle, that edge isn't coastline."""
    store, cache = dirs
    # Two stored rectangles meet at lat 1, inside the box, with land across it.
    landmask.ensure_landmasks(
        [(0.0, 1.0, 0.0, 1.0), (0.0, 1.0, 1.0, 2.0)],
        store,
        cache,
    )
    _land, _covered, coastline = landmask.load_landmask(
        (0.0, 1.0, 0.0, 2.0),
        str(store),
        str(cache),
    )
    assert coastline.length == pytest.approx(2.0, abs=EDGE_GAP)
    assert coastline.bounds == pytest.approx(
        (COAST_LON, 0.0, COAST_LON, 2.0),
        abs=EDGE_GAP,
    )


def test_missing_parts_are_filled_from_the_database_tiles(dirs, fetches):
    """A part the store lacks is fetched as the database tile that reaches it."""
    store, cache = dirs
    tiles = [(0.0, 0.5, 0.0, 0.5, 20230101, 20230201), (5.0, 6.0, 5.0, 6.0)]
    landmask.load_landmask((0.1, 0.4, 0.1, 0.4), str(store), str(cache), tiles)
    assert fetches == [[0.0, 0.5, 0.0, 0.5]]


def test_parts_beyond_the_database_are_filled_in_whole_degree_cells(dirs, fetches):
    """Outside every database tile, the store is filled one degree cell at a time."""
    store, cache = dirs
    _land, covered, _coastline = landmask.load_landmask(
        (0.5, 1.5, 0.2, 0.8),
        str(store),
        str(cache),
    )
    assert sorted(fetches) == [[0.0, 1.0, 0.0, 1.0], [1.0, 2.0, 0.0, 1.0]]
    assert covered.area == pytest.approx(0.6)


def test_with_no_landmask_to_be_had_everything_is_empty(dirs, fetches):
    """When nothing can be fetched, there is no land and nothing counts as covered."""
    store, cache = dirs
    fetches.fail = True
    land, covered, coastline = landmask.load_landmask(
        (0.0, 1.0, 0.0, 1.0),
        str(store),
        str(cache),
    )
    assert land.is_empty
    assert covered.is_empty
    assert coastline.is_empty


def test_only_the_covered_part_of_the_box_counts_as_covered(dirs, fetches):
    """A part of the box no landmask covers is neither land nor water."""
    store, cache = dirs
    landmask.ensure_landmasks([(0.0, 1.0, 0.0, 1.0)], store, cache)
    fetches.fail = True
    land, covered, _coastline = landmask.load_landmask(
        (0.0, 2.0, 0.0, 1.0),
        str(store),
        str(cache),
    )
    assert covered.symmetric_difference(shapely.box(0, 0, 1, 1)).area == (
        pytest.approx(0.0)
    )
    assert land.symmetric_difference(shapely.box(0, 0, COAST_LON, 1)).area == (
        pytest.approx(0.0)
    )
