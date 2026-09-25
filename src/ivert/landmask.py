"""A local store of OpenStreetMap landmasks, kept alongside the photon database.

While building the database, globato fetches an OSM coastline landmask for each
download request and caches it (fetchez's 'osm_landmask' module). IVERT keeps its
own copy of the land for each of the database's storage tiles in the
'ivert_landmask_directory', clipped to the tile, so that a validation can build a
landmask from local files instead of querying OSM again. The store lives with the
database rather than in the cache, so clearing the cache does not lose it.

A rectangle is added to the store from whatever cached OSM landmasks already
cover it, and fetched from OSM only if they do not. A stored file with no land in
it is open water, which is still a covered area.
"""

import contextlib
import glob
import json
import logging
import math
import os
import pathlib
import re
import tempfile

import numpy as np
import pyogrio
import shapely
import shapely.geometry

import ivert.utils.logging_config

logger = logging.getLogger(__name__)

# Stored files are named for the rectangle they cover, in IVERT's xmin/xmax/ymin/ymax
# (W/E/S/N) order.
_STORE_FILE_RE = re.compile(
    r"^landmask_(-?[\d.]+)_(-?[\d.]+)_(-?[\d.]+)_(-?[\d.]+)\.geojson$",
)

# The cached files fetchez's 'osm_landmask' module writes with its default options,
# named for their W/S/E/N rectangle.
_OSM_CACHE_FILE_RE = re.compile(
    r"^osm_landmask_(-?[\d.]+)_(-?[\d.]+)_(-?[\d.]+)_(-?[\d.]+)_e_binary\.geojson$",
)

# Where fetchez puts 'osm_landmask' files under a given output directory. Older
# globato versions wrote them one level down, in 'icesat2/'.
_OSM_CACHE_SUBDIRS = ("osm_landmask", os.path.join("icesat2", "osm_landmask"))

# Area that is not covered by any landmask, in square degrees, below which it is
# treated as rounding and ignored.
_AREA_TOLERANCE = 1e-9

# Area outside every database storage tile is filled in cells of this many degrees,
# aligned to the whole degree, so neighbouring validations share them.
_FILL_CELL_DEG = 1.0


def _box(bbox):
    """A shapely box from an (xmin, xmax, ymin, ymax) rectangle."""
    xmin, xmax, ymin, ymax = bbox
    return shapely.box(xmin, ymin, xmax, ymax)


def store_filename(store_dir: str, bbox) -> str:
    """The stored landmask file for an (xmin, xmax, ymin, ymax) rectangle."""
    xmin, xmax, ymin, ymax = (float(v) for v in bbox)
    return os.path.join(store_dir, f"landmask_{xmin}_{xmax}_{ymin}_{ymax}.geojson")


def stored_landmasks(store_dir: str) -> dict:
    """{path: (xmin, xmax, ymin, ymax)} of every landmask in the store."""
    found = {}
    for path in glob.glob(os.path.join(store_dir, "landmask_*.geojson")):
        match = _STORE_FILE_RE.match(os.path.basename(path))
        if match:
            found[path] = tuple(float(v) for v in match.groups())
    return found


def _cached_osm_landmasks(cache_dir: str) -> dict:
    """{path: (xmin, xmax, ymin, ymax)} of the OSM landmasks fetchez has cached."""
    found = {}
    for subdir in _OSM_CACHE_SUBDIRS:
        pattern = os.path.join(cache_dir, subdir, "osm_landmask_*.geojson")
        for path in glob.glob(pattern):
            match = _OSM_CACHE_FILE_RE.match(os.path.basename(path))
            if match:
                west, south, east, north = (float(v) for v in match.groups())
                found[path] = (west, east, south, north)
    return found


def _read_polygons(path: str, bbox) -> list:
    """The polygons of a landmask file that reach into an (xmin, xmax, ymin, ymax) box."""
    xmin, xmax, ymin, ymax = bbox
    _, _, wkb, _ = pyogrio.raw.read(
        path,
        columns=[],
        read_geometry=True,
        bbox=(xmin, ymin, xmax, ymax),
    )
    if wkb is None or len(wkb) == 0:
        return []
    return list(shapely.make_valid(shapely.from_wkb(wkb)))


def land_from_files(paths, bbox):
    """The land of several landmask files, merged and clipped to bbox."""
    polygons = []
    for path in paths:
        polygons.extend(_read_polygons(path, bbox))
    if not polygons:
        return shapely.Polygon()
    return shapely.union_all(polygons).intersection(_box(bbox))


def write_land(path: str, land) -> None:
    """Write land polygons to a GeoJSON file, atomically.

    Validations of neighbouring DEMs may run at the same time and fill the same
    rectangle; the rename means none of them ever reads a half-written file.
    """
    polygons = [
        g
        for g in shapely.get_parts(land)
        if isinstance(g, shapely.Polygon) and not g.is_empty
    ]
    features = [
        {"type": "Feature", "properties": {}, "geometry": shapely.geometry.mapping(g)}
        for g in polygons
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".geojson.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": features}, f)
        pathlib.Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def clip_stored_landmask(path: str, bbox, store_dir: str) -> str:
    """Write the part of a stored landmask inside an (xmin, xmax, ymin, ymax) box.

    The result is a stored landmask of its own, named for bbox, in store_dir.
    Returns its path.
    """
    out = store_filename(store_dir, bbox)
    write_land(out, land_from_files([path], bbox))
    return out


def _fill_from_cache(bbox, store_dir: str, cache_dir: str) -> bool:
    """Store the landmask of bbox from cached OSM landmasks, if they cover all of it."""
    need = _box(bbox)
    cached = {
        p: b
        for p, b in _cached_osm_landmasks(cache_dir).items()
        if _box(b).intersects(need)
    }
    if not cached:
        return False
    if need.difference(shapely.union_all([_box(b) for b in cached.values()])).area > (
        _AREA_TOLERANCE
    ):
        return False
    write_land(store_filename(store_dir, bbox), land_from_files(cached, bbox))
    return True


def _fetch_osm_landmask(bbox, cache_dir: str) -> None:
    """Fetch the OSM landmask of bbox into the cache, the way globato does."""
    import fetchez

    xmin, xmax, ymin, ymax = bbox
    with ivert.utils.logging_config.keep_root_logging():
        fetchez.get(
            "osm_landmask",
            region=[xmin, xmax, ymin, ymax],
            outdir=cache_dir,
            verbose=False,
        )


def ensure_landmasks(tiles, store_dir: str, cache_dir: str, fetch: bool = True) -> list:
    """Make sure the store holds a landmask for each rectangle in tiles.

    A rectangle missing from the store is filled from the cached OSM landmasks if
    they cover it, else (if fetch) from OSM, via the cache. A failed fetch is logged
    and the rectangle left out.

    Args:
        tiles: (xmin, xmax, ymin, ymax, ...) rectangles in WGS84. Any values past the
            fourth (such as a date range) are ignored.
        store_dir: the landmask store (the 'ivert_landmask_directory' setting).
        cache_dir: where fetchez caches OSM landmasks (IVERT's
            'icesat2_download_directory', which globato uses too).
        fetch: whether to query OSM for rectangles the cache does not cover.

    Returns:
        The rectangles added to the store.

    """
    stored = set(stored_landmasks(store_dir).values())
    added = []
    for tile in tiles:
        bbox = tuple(float(v) for v in tile[:4])
        if bbox in stored:
            continue
        if not _fill_from_cache(bbox, store_dir, cache_dir):
            if not fetch:
                continue
            logger.info(
                "Fetching the OSM landmask for %s.",
                "/".join(f"{v:g}" for v in bbox),
            )
            try:
                _fetch_osm_landmask(bbox, cache_dir)
            except Exception:
                logger.warning(
                    "Could not fetch the OSM landmask for %s.",
                    "/".join(f"{v:g}" for v in bbox),
                    exc_info=True,
                )
                continue
            if not _fill_from_cache(bbox, store_dir, cache_dir):
                logger.warning(
                    "No OSM landmask came back for %s.",
                    "/".join(f"{v:g}" for v in bbox),
                )
                continue
        stored.add(bbox)
        added.append(bbox)
    return added


def _fill_cells(area) -> list:
    """Whole-degree cells, (xmin, xmax, ymin, ymax), that reach into area."""
    step = _FILL_CELL_DEG
    xmin, ymin, xmax, ymax = area.bounds
    cells = []
    for x in np.arange(math.floor(xmin / step) * step, xmax, step):
        for y in np.arange(math.floor(ymin / step) * step, ymax, step):
            cell = (float(x), float(x + step), float(y), float(y + step))
            if _box(cell).intersection(area).area > _AREA_TOLERANCE:
                cells.append(cell)
    return cells


def load_landmask(bbox, store_dir: str, cache_dir: str, database_tiles=()):
    """The landmask over an (xmin, xmax, ymin, ymax) box, from the store.

    Parts of bbox the store does not cover are added to it first: the database
    storage tiles that reach them (from the cache if possible, else from OSM), then
    whole-degree cells for anything beyond the database.

    Args:
        bbox: the (xmin, xmax, ymin, ymax) area wanted, in WGS84.
        store_dir: the landmask store.
        cache_dir: where fetchez caches OSM landmasks.
        database_tiles: (xmin, xmax, ymin, ymax, ...) storage tiles of the photon
            database, used as the unit of anything fetched.

    Returns:
        (land, covered, coastline): the land, clipped to bbox; the part of bbox any
        landmask covers (outside it a point is neither land nor water); and the real
        shoreline, as lines. Boundaries between stored rectangles and the edges of
        rectangles and of bbox are not shoreline.

    """
    need = _box(bbox)

    def missing_area():
        extents = [_box(b) for b in stored_landmasks(store_dir).values()]
        return need.difference(shapely.union_all(extents)) if extents else need

    missing = missing_area()
    if missing.area > _AREA_TOLERANCE:
        tiles = [t for t in database_tiles if _box(t[:4]).intersects(missing)]
        ensure_landmasks(tiles, store_dir, cache_dir)
        missing = missing_area()
    if missing.area > _AREA_TOLERANCE:
        ensure_landmasks(_fill_cells(missing), store_dir, cache_dir)

    extents = {
        p: _box(b)
        for p, b in stored_landmasks(store_dir).items()
        if _box(b).intersects(need)
    }
    if not extents:
        return shapely.Polygon(), shapely.Polygon(), shapely.MultiLineString([])

    land = land_from_files(extents, bbox)
    covered = shapely.union_all(list(extents.values())).intersection(need)

    edges = shapely.union_all([e.boundary for e in extents.values()] + [need.boundary])
    coastline = land.boundary.difference(edges.buffer(1e-6))

    shapely.prepare(land)
    shapely.prepare(covered)
    return land, covered, coastline
