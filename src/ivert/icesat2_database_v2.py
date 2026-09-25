# Functionality for reading ICESat-2 data and saving it in a tiled database.

# Import netCDF4 first to force C-library symbol resolution before h5py/xarray loads.
# Bypasses HDF5 dimscale corruption in pip/venv environments.
# This library isn't used directly in this file, but we need to import it
# in order for xarray to initialize correctly.
import netCDF4  # noqa: F401

# isort: split

import contextlib
import datetime
import itertools
import logging
import multiprocessing
import os
import queue
import re
import shutil
import sys
from collections.abc import Iterator
from typing import ClassVar, NamedTuple

import dateparser
import fetchez
import fetchez.core
import fetchez.spatial
import globato
import numpy as np
import pandas as pd
import psutil
import shapely
import shapely.prepared
import tqdm
import tqdm.contrib.logging
import xarray
from fetchez.modules.earthdata import IceSat2 as _FetchezIceSat2

import ivert.landmask
import ivert.utils.configfile
import ivert.utils.cuboid_funcs
from ivert.icesat2_requests import ICESat2RequestsCSV

logger = logging.getLogger(__name__)

# ICESat-2 epoch: all delta_time values are seconds since 2018-01-01T00:00:00Z
_ICESAT2_EPOCH = datetime.datetime(2018, 1, 1, 0, 0, 0, tzinfo=datetime.UTC)


class DownloadSummary(NamedTuple):
    """The outcome of a call to :meth:`IS2Database.download_new_granules`.

    A download is split into one sub-region ("part") per tile, and each part is
    counted in exactly one of the first three fields. ``parts_failed`` counts
    only real errors -- a Harmony request that came back with nothing -- while
    ``parts_empty`` counts the parts Harmony served correctly that added no new
    granules, either because the region holds no ICESat-2 data or because every
    granule over it is already in the database. Only ``parts_failed`` means
    something went wrong. Callers use this to decide an exit status; nothing
    here reflects how *much* of the region was covered.
    """

    parts_downloaded: int = 0
    parts_empty: int = 0
    parts_failed: int = 0
    granules_added: int = 0

    @property
    def parts_attempted(self) -> int:
        """The number of sub-regions the download tried to fetch."""
        return self.parts_downloaded + self.parts_empty + self.parts_failed


def _yyyymmdd_to_delta_time(yyyymmdd: int | str) -> float:
    """Convert a YYYYMMDD integer to ICESat-2 delta_time (seconds since 2018-01-01)."""
    return (
        datetime.datetime.strptime(str(int(yyyymmdd)), "%Y%m%d").replace(
            tzinfo=datetime.UTC,
        )
        - _ICESAT2_EPOCH
    ).total_seconds()


def _delta_time_to_yyyymmdd(delta_time: float) -> int:
    """Convert ICESat-2 delta_time (seconds since 2018-01-01) to a YYYYMMDD integer."""
    return int(
        (_ICESAT2_EPOCH + datetime.timedelta(seconds=float(delta_time))).strftime(
            "%Y%m%d",
        ),
    )


# --- Sizing the classification worker pool ----------------------------------
# A subset is classified in memory: its photons expand into ~16 float64 columns
# plus the classifiers' temporaries. Measured on the largest subset of a Southern
# California run (a 448 MiB file), the process grew by 1.7 GB, about 4x the file;
# 6x leaves headroom for denser granules.
_CLASSIFY_BYTES_PER_H5_BYTE = 6
_CLASSIFY_MIN_WORKER_BYTES = 1 << 30
# Beyond this many workers the NSIDC downloads and the disk become the limit.
_CLASSIFY_MAX_WORKERS = 8
# The share of the memory available right now that the pool may plan on using.
_CLASSIFY_MEMORY_SHARE = 0.6
# Workers run at this niceness so an interactive machine stays responsive.
_CLASSIFY_NICE = 5


def classify_worker_count(
    setting,
    h5_files,
    *,
    cpu_count: int | None = None,
    available_bytes: int | None = None,
    fork_available: bool | None = None,
) -> tuple[int, str]:
    """Decide how many processes should classify a request's granules, and why.

    ``setting`` is the ``icesat2_classify_workers`` option. An integer forces that
    many (1 is the serial path). "auto" sizes the pool from the machine so that a
    laptop is not overloaded: one fewer than its cores, no more than the memory
    available now allows at an estimated `_CLASSIFY_BYTES_PER_H5_BYTE` times the
    largest file per process, and at most `_CLASSIFY_MAX_WORKERS`. Workers are
    forked so that mask trees already built in the parent are shared rather than
    rebuilt; where fork is unavailable the answer is 1.

    Args:
        setting: The configured value, an integer or "auto".
        h5_files: The subset files to be classified; the largest sets the memory
            estimate.
        cpu_count: Stands in for ``os.cpu_count()`` (tests).
        available_bytes: Stands in for ``psutil.virtual_memory().available`` (tests).
        fork_available: Stands in for the platform check (tests).

    Returns:
        ``(count, reason)``, the reason being a short phrase for the log.
    """
    if setting is not None and str(setting).strip().lower() != "auto":
        try:
            forced = int(setting)
        except (TypeError, ValueError):
            logger.warning(
                "icesat2_classify_workers=%r is neither an integer nor 'auto'; "
                "sizing the pool automatically.",
                setting,
            )
        else:
            return max(1, forced), f"icesat2_classify_workers = {forced}"

    if fork_available is None:
        fork_available = (
            sys.platform.startswith("linux")
            and "fork" in multiprocessing.get_all_start_methods()
        )
    if not fork_available:
        return 1, "this platform cannot fork worker processes"

    if cpu_count is None:
        cpu_count = os.cpu_count() or 1
    if available_bytes is None:
        available_bytes = psutil.virtual_memory().available
    largest = max((os.path.getsize(f) for f in h5_files), default=0)
    per_worker = max(_CLASSIFY_MIN_WORKER_BYTES, _CLASSIFY_BYTES_PER_H5_BYTE * largest)

    by_cpu = max(1, cpu_count - 1)
    by_memory = max(1, int(_CLASSIFY_MEMORY_SHARE * available_bytes // per_worker))
    count = min(by_cpu, by_memory, _CLASSIFY_MAX_WORKERS)
    reason = (
        f"{cpu_count} cores, {available_bytes / 2**30:.1f} GB available, "
        f"~{per_worker / 2**30:.1f} GB per worker for the largest file"
    )
    return count, reason


# --- Fetching ATL08/ATL24 ahead of classification ------------------------------
# globato fetches a granule's ATL08 and ATL24 when it classifies it, one granule
# at a time, and the two together are often a couple of hundred megabytes: with
# the classification itself down to seconds, waiting for them is most of a
# granule's time. They are fetched ahead instead, this many at once.
_AUX_PREFETCH_THREADS = 6
_AUX_PRODUCTS = ("ATL08", "ATL24")
# How long the parent waits for the prefetch child's next report before
# checking that the child is still alive.
_READY_POLL_SECONDS = 5.0


def _prefetch_aux_granules(h5_files, cache_dir, threads: int, ready) -> None:
    """Fetch the ATL08 and ATL24 granules for each subset, several at a time.

    Runs in a process of its own (see :meth:`IS2Database._start_aux_prefetch`),
    through globato's own lookup, so the files land exactly where its reader
    later looks for them. A file already in the cache costs a directory glob.
    Each subset's path is put on ``ready`` once its files have been looked for,
    whether or not any were found, and ``None`` once all have.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from globato.streams.readers.icesat2 import ATL03Reader

    def fetch(h5_fn):
        reader = ATL03Reader(h5_fn, cache_dir=cache_dir, classes="1")
        return [reader.fetch_atlxx(h5_fn, name) for name in _AUX_PRODUCTS]

    counts = [0] * len(_AUX_PRODUCTS)
    try:
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = {pool.submit(fetch, h5_fn): h5_fn for h5_fn in h5_files}
            for future in as_completed(futures):
                try:
                    found = future.result()
                except Exception:
                    logger.warning(
                        "Fetching aux granules for %s failed; its classification "
                        "will fetch them itself.",
                        os.path.basename(futures[future]),
                        exc_info=True,
                    )
                else:
                    for i, path in enumerate(found):
                        counts[i] += path is not None
                ready.put(futures[future])
    finally:
        ready.put(None)
    logger.info(
        "Aux granules ready for %d subsets: %s.",
        len(h5_files),
        ", ".join(f"{n} {name}" for name, n in zip(_AUX_PRODUCTS, counts, strict=True)),
    )


# The database object a forked classification worker works through.
_WORKER_DB = None


def _start_classify_worker(config, nice: int) -> None:
    """Set up a forked classification worker: one database object, lower priority."""
    global _WORKER_DB
    with contextlib.suppress(AttributeError, OSError):
        os.nice(nice)
    _WORKER_DB = IS2Database(ivert_config=config)


def _classify_in_worker(job: tuple) -> tuple:
    """Classify one subset in a worker; ``job`` is (granule index, kwargs)."""
    index, kwargs = job
    return index, kwargs["h5_fn"], _WORKER_DB._process_h5_to_nc_tiles(**kwargs)


class DatabaseNotFoundError(Exception):
    """No IVERT photon database exists where the configuration points.

    Raised by IS2Database.ensure_index_exists() when neither an index file nor
    any granules are found, i.e. when there is nothing to query and nothing to
    rebuild an index from.
    """


class IS2Database:
    # Column groups describing how the index is serialized to the NetCDF file.
    # Every column becomes a plain 1-D numeric or string variable over the
    # "record" dimension, so the index reads back as an ordinary DataFrame with
    # no shapely geometry to reconstruct. The bounding boxes are stored exploded
    # into scalar columns (query_bbox_xmin, ..._tmax, data_bbox_xmin, ...) so
    # spatial lookups are vectorized numpy comparisons rather than per-row
    # shapely/geometry work. The polygon footprint is not stored: it is always
    # box(data_bbox), trivially rebuilt from the data_bbox_* columns if needed.
    _INDEX_STR_COLS = (
        "granule_id",
        "filename",
        "source_granule",
        "laser_name",
        "horizontal_datum",
        "vertical_datum",
    )
    _INDEX_INT_COLS = (
        "numphotons",
        "numphotons_unclassified",
        "numphotons_noise",
        "numphotons_ground",
        "numphotons_canopy",
        "numphotons_canopy_top",
        "numphotons_ice_surface",
        "numphotons_bathy_floor",
        "numphotons_bathy_surface",
        "numphotons_buildings",
        "numphotons_inland_water_surface",
        "downloaded_on_utc",
    )
    # Index columns renamed since earlier versions, mapped new name -> old name.
    # Index files and granule .nc attrs written before a rename still carry the old
    # name, so both read paths fall back to it rather than erroring (index) or
    # silently defaulting to 0 (granule attrs). Note that pre-rename
    # "downloaded_on" values are machine-local dates, not UTC ones.
    _INDEX_LEGACY_COL_NAMES: ClassVar[dict[str, str]] = {
        "downloaded_on_utc": "downloaded_on",
    }
    # Bounding-box bases and per-axis suffixes. xmin/xmax/ymin/ymax are floats;
    # tmin/tmax are YYYYMMDD ints (the box's date range).
    _BBOX_BASES = ("query_bbox", "data_bbox")
    _INDEX_BBOX_FLOAT_COLS = (
        "query_bbox_xmin",
        "query_bbox_xmax",
        "query_bbox_ymin",
        "query_bbox_ymax",
        "data_bbox_xmin",
        "data_bbox_xmax",
        "data_bbox_ymin",
        "data_bbox_ymax",
    )
    _INDEX_BBOX_INT_COLS = (
        "query_bbox_tmin",
        "query_bbox_tmax",
        "data_bbox_tmin",
        "data_bbox_tmax",
    )
    _INDEX_ZBOUNDS_COLS = ("zbounds_zmin", "zbounds_zmax")

    @classmethod
    def _stored_col_name(cls, col: str, source) -> str:
        """Return the name `col` is actually stored under in `source`.

        `source` is anything supporting `in` over its field names (an xarray
        Dataset read from an index file, or a granule's global-attrs dict). Falls
        back to the pre-rename name when only that one is present, so files
        written by older versions still read correctly. Returns `col` unchanged
        when neither is present, leaving the caller's own missing-field handling
        (a KeyError, or a .get() default) to apply.
        """
        if col in source:
            return col
        legacy = cls._INDEX_LEGACY_COL_NAMES.get(col)
        if legacy is not None and legacy in source:
            return legacy
        return col

    @staticmethod
    def _bbox_cols(base: str) -> tuple[str, ...]:
        """Return the six scalar column names for a bbox base, in canonical order."""
        return (
            f"{base}_xmin",
            f"{base}_xmax",
            f"{base}_ymin",
            f"{base}_ymax",
            f"{base}_tmin",
            f"{base}_tmax",
        )

    @classmethod
    def _bbox_to_cols(cls, base: str, bbox) -> dict:
        """Explode a 6-element [xmin, xmax, ymin, ymax, tmin, tmax] bbox to scalars."""
        c = cls._bbox_cols(base)
        return {
            c[0]: float(bbox[0]),
            c[1]: float(bbox[1]),
            c[2]: float(bbox[2]),
            c[3]: float(bbox[3]),
            c[4]: int(bbox[4]),
            c[5]: int(bbox[5]),
        }

    def __init__(
        self,
        ivert_config: ivert.utils.configfile.Config | None = None,
    ) -> None:
        # Define the structure of the object.
        if ivert_config is None:
            self.config = ivert.utils.configfile.Config()
        else:
            self.config = ivert_config

        self.db_fname = self.config.ivert_database_index
        self.gdf = None
        self.last_gdf_bbox = None
        self.last_gdf_date_range = None
        self.last_gdf_result = None

        # For now, we only build this database in WGS84 / EGM2008 coordinates.
        # Can experiment with other coordinate systems later.
        self.crs = "EPSG:4326+3855"

        self.granules_dir = self.config.ivert_database_directory
        self.icesat2_download_dir = self.config.icesat2_download_directory
        self.landmask_dir = self.config.ivert_landmask_directory

    def create_new_database(
        self,
        populate: bool = True,
        overwrite: bool = False,
    ) -> pd.DataFrame:
        """Create a new database from scratch.

        Args:
            populate: Whether to populate the database with the data from the tiles.
            overwrite: Whether to overwrite the database if it already exists.

        Raises:
            OSError if database file cannot be created or already exists and overwrite is False.

        Returns:
            pandas.DataFrame containing the granule records from the database.

        """
        if overwrite:
            if os.path.exists(self.db_fname):
                logger.info("Removing old %s", os.path.basename(self.db_fname))
                os.remove(self.db_fname)

        elif os.path.exists(self.db_fname):
            raise OSError(
                "Database file already exists. Use overwrite=True to overwrite it.",
            )

        if populate:
            nc_files = self.granule_files()

            records = []
            # 'disable=None' tells tqdm to draw the bar only when attached to a
            # terminal, and stay silent when output is redirected to a file or a
            # pipe. Log records are routed through tqdm.write() meanwhile, so a
            # warning about an unreadable file doesn't break the bar.
            with tqdm.contrib.logging.logging_redirect_tqdm():
                for nc_fn in tqdm.tqdm(
                    nc_files,
                    disable=None if logger.isEnabledFor(logging.INFO) else True,
                    unit="file",
                    desc="Reading granules",
                ):
                    meta = self._read_nc_metadata(nc_fn)
                    if meta is not None:
                        records.append(meta)

            if records:
                gdf = pd.DataFrame(records)[list(self._empty_db_dict().keys())]
            else:
                gdf = pd.DataFrame(self._empty_db_dict()).drop(
                    labels=0,
                    axis="rows",
                )

        else:
            gdf = pd.DataFrame(self._empty_db_dict()).drop(labels=0, axis="rows")

        self._write_index(gdf)
        if os.path.exists(self.db_fname):
            logger.debug(
                "Created %s with %d records.",
                os.path.basename(self.db_fname),
                len(gdf),
            )
        else:
            raise OSError("Failed to create", os.path.basename(self.db_fname))

        # This becomes the new database for this object.
        self.gdf = gdf

        return gdf

    def granule_files(self) -> list[str]:
        """Return the paths of the .nc granule files in the granules directory, sorted.

        The index file lives in that same directory and also ends in .nc, so it
        is explicitly skipped. Returns an empty list if the directory does not
        exist.
        """
        if not os.path.isdir(self.granules_dir):
            return []

        index_fn = os.path.basename(self.db_fname)
        return sorted(
            os.path.join(self.granules_dir, fn)
            for fn in os.listdir(self.granules_dir)
            if os.path.splitext(fn)[-1].lower() == ".nc" and fn != index_fn
        )

    def ensure_index_exists(self) -> None:
        """Check that this database's index file is in place before it is queried.

        A query only ever touches the index, so an index that has been deleted
        or was never built makes an otherwise-complete database look empty and
        fails deep inside the query with an unhelpful traceback. Check it up
        front instead:

          * index present                       -> nothing to do.
          * index missing, but granules on disk -> warn and rebuild it in place.
          * neither one                         -> DatabaseNotFoundError.

        Raises:
            DatabaseNotFoundError if no index file exists and there are no granules
            to rebuild it from.

        """
        if os.path.exists(self.db_fname):
            return

        granule_fnames = self.granule_files()

        if not granule_fnames:
            raise DatabaseNotFoundError(
                "No IVERT ICESat-2 photon database exists where IVERT is looking "
                "for it. Neither the index file nor any .nc granule files were found.\n"
                f"  index file:         {self.db_fname}\n"
                f"  granules directory: {self.granules_dir}\n"
                "Run 'ivert database download <bbox or DEM>' to begin creating an IVERT "
                "database, or point IVERT at an existing one with "
                "'ivert options ivert_database_directory=<path>'.",
            )

        logger.warning(
            "The database index '%s' is missing, but %d granule file(s) are present "
            "in %s. Rebuilding the index in place; this may take a few moments.",
            os.path.basename(self.db_fname),
            len(granule_fnames),
            self.granules_dir,
        )
        self.create_new_database(populate=True, overwrite=True)

    @classmethod
    def _empty_db_dict(cls) -> dict:
        """Return a single-row dict defining the index schema and canonical column order.

        Used to construct a blank DataFrame and as the single source of truth for
        column ordering (its key order) throughout the index read/write code.
        """
        d = {
            "granule_id": ["placeholder"],
            "filename": ["placeholder"],
            "source_granule": ["placeholder"],
            "laser_name": ["all"],
        }
        for base in cls._BBOX_BASES:
            for col in cls._bbox_cols(base):
                d[col] = [0]
        d["zbounds_zmin"] = [0.0]
        d["zbounds_zmax"] = [0.0]
        for col in cls._INDEX_INT_COLS:
            d[col] = [0]
        d["horizontal_datum"] = ["EPSG:4326"]
        d["vertical_datum"] = ["EPSG:4979"]
        return d

    @classmethod
    def _index_record_from_attrs(cls, attrs: dict, filename: str) -> dict:
        """Build a scalar-column index record from a granule's (list-form) attrs.

        `attrs` holds granule metadata in the same shape as the .nc granule-file
        global attributes: query_bbox / data_bbox as 6-element sequences and
        zbounds as a 2-element sequence. The result is a flat dict of scalar
        values matching the index schema (see _empty_db_dict). The polygon
        footprint is omitted; it is always box(data_bbox).
        """
        record = {
            "granule_id": str(
                attrs.get("granule_id", os.path.splitext(filename)[0]),
            ),
            "filename": filename,
            "source_granule": str(
                attrs.get(
                    "source_granule",
                    cls._source_granule_from_filename(filename),
                ),
            ),
            "laser_name": str(attrs.get("laser_name", "all")),
        }
        record.update(
            cls._bbox_to_cols(
                "query_bbox",
                attrs.get("query_bbox", [0.0, 0.0, 0.0, 0.0, 0, 0]),
            ),
        )
        record.update(cls._bbox_to_cols("data_bbox", attrs["data_bbox"]))
        zb = attrs.get("zbounds", [float("nan"), float("nan")])
        record["zbounds_zmin"] = float(zb[0])
        record["zbounds_zmax"] = float(zb[1])
        for col in cls._INDEX_INT_COLS:
            record[col] = int(attrs.get(cls._stored_col_name(col, attrs), 0))
        record["horizontal_datum"] = str(attrs.get("horizontal_datum", ""))
        record["vertical_datum"] = str(attrs.get("vertical_datum", ""))
        return record

    @classmethod
    def _read_nc_metadata(cls, nc_fn: str) -> dict | None:
        """Read metadata attrs from a NetCDF granule file without loading photon arrays.

        This is deliberately fast: xarray reads only the file header, not the data.
        Returns a scalar-column index record (see _index_record_from_attrs), or
        None if the file's metadata can't be read.
        """
        try:
            with xarray.open_dataset(nc_fn) as ds:
                attrs = dict(ds.attrs)
            return cls._index_record_from_attrs(attrs, os.path.basename(nc_fn))
        except Exception as e:
            logger.warning(
                "Could not read metadata from %s: %s",
                os.path.basename(nc_fn),
                e,
            )
            return None

    @staticmethod
    def _query_bbox_suffix(query_bbox: tuple) -> str:
        """Return the filename suffix that identifies a query bounding box.

        Format: _<W|E><xmin>_<W|E><xmax>_<S|N><ymin>_<S|N><ymax>_<tmin>_<tmax>
        """

        def _lon_tag(v):
            return f"{'W' if v < 0 else 'E'}{abs(float(v)):09.5f}"

        def _lat_tag(v):
            return f"{'S' if v < 0 else 'N'}{abs(float(v)):08.5f}"

        xmin, xmax, ymin, ymax, tmin, tmax = query_bbox
        return (
            f"_{_lon_tag(xmin)}_{_lon_tag(xmax)}"
            f"_{_lat_tag(ymin)}_{_lat_tag(ymax)}"
            f"_{int(tmin)}_{int(tmax)}"
        )

    # The suffix built by _query_bbox_suffix(), at the end of a filename's stem.
    _QUERY_SUFFIX_RE = re.compile(
        r"_[EW]\d{3}\.\d{5}_[EW]\d{3}\.\d{5}_[NS]\d{2}\.\d{5}_[NS]\d{2}\.\d{5}_\d+_\d+$",
    )

    @classmethod
    def _granule_stem(cls, filename: str) -> str:
        """Return a granule's filename without directory, extension or query suffix."""
        stem = os.path.splitext(os.path.basename(filename))[0]
        return cls._QUERY_SUFFIX_RE.sub("", stem)

    @classmethod
    def _subset_cache_filename(cls, h5_fn: str, query_bbox: tuple) -> str:
        """Name a downloaded Harmony subset after the granule and the box it was cut to.

        Harmony names every subset of a granule alike, whatever box it was cut to,
        and fetchez keeps any file that already exists rather than fetching it again.
        Adjacent parts of a request share granules, so under Harmony's name the second
        part would read the first part's subset and find none of its own photons in
        it. The query suffix keeps each part's subsets apart. The granule id fields
        stay in front, where globato and the release check read them.
        """
        return cls._granule_stem(h5_fn) + cls._query_bbox_suffix(query_bbox) + ".h5"

    def storage_tiles(self) -> list[tuple[float, float, float, float]]:
        """The distinct (xmin, xmax, ymin, ymax) storage tiles the database holds."""
        gdf = self.open_gdf()
        if gdf is None or len(gdf) == 0:
            return []
        cols = [
            "query_bbox_xmin",
            "query_bbox_xmax",
            "query_bbox_ymin",
            "query_bbox_ymax",
        ]
        return [
            tuple(float(v) for v in row)
            for row in gdf[cols].drop_duplicates().to_numpy()
        ]

    def _store_landmasks(self, storage_tiles) -> None:
        """Copy the cached OSM landmask of each storage tile into the landmask store.

        Only from the cache, which globato has just filled for these tiles; a tile
        it could not fill is left for a validation to fetch. Never fails a download.
        """
        try:
            ivert.landmask.ensure_landmasks(
                storage_tiles,
                self.landmask_dir,
                self.icesat2_download_dir,
                fetch=False,
            )
        except Exception:
            logger.warning(
                "Could not store the landmasks of this part; validations will "
                "rebuild them when needed.",
                exc_info=True,
            )

    @classmethod
    def _nc_filename(cls, h5_fn: str, query_bbox: tuple) -> str:
        """Build a unique .nc filename by appending the query bbox to the granule base name.

        Format: <granule_base>_<W|E><xmin>_<W|E><xmax>_<S|N><ymin>_<S|N><ymax>_<tmin>_<tmax>.nc

        This ensures that the same granule downloaded for different query regions or time
        spans produces distinct files rather than overwriting each other. A query suffix
        already on the h5 name (see _subset_cache_filename) is not repeated.
        """
        return cls._granule_stem(h5_fn) + cls._query_bbox_suffix(query_bbox) + ".nc"

    # Matches the "_<W|E><xmin>_<W|E><xmax>_<S|N><ymin>_<S|N><ymax>_<tmin>_<tmax>.nc" suffix
    # appended by _nc_filename(), so the source granule id can be recovered from any nc
    # filename regardless of which query bbox/dates produced it.
    _NC_SUFFIX_RE = re.compile(
        r"_[EW]\d{3}\.\d{5}_[EW]\d{3}\.\d{5}_[NS]\d{2}\.\d{5}_[NS]\d{2}\.\d{5}_\d+_\d+\.nc$",
    )

    @classmethod
    def _source_granule_from_filename(cls, filename: str) -> str:
        """Recover the original NASA granule id from a database/nc filename.

        Strips the bbox/date suffix appended by _nc_filename(), so that the same granule
        downloaded under different query regions or dates is still recognized as the same
        underlying source data (used to match records for --replace).
        """
        return cls._NC_SUFFIX_RE.sub("", filename)

    @staticmethod
    def _h5_along_track_m(h5_fn: str, beams) -> dict:
        """Read what is needed to give each photon of the given beams its along-track distance.

        Cumulative along-track distance is the sum of ``segment_length`` over the
        geolocation segments before the photon's, plus the photon's own
        ``dist_ph_along`` within it. The photons of a beam are stored segment
        after segment, ``segment_ph_cnt[i]`` of them for segment ``i``, which is
        how globato numbers them too (``ph_index_within_seg``).

        Returns ``{beam: (segment_id, segment_start_row, along_track_m)}`` with
        ``along_track_m`` in photon (heights) order; a beam missing from the file
        is left out.
        """
        import h5py

        tables = {}
        with h5py.File(h5_fn, "r") as f:
            for beam in beams:
                try:
                    dist_ph_along = f[f"{beam}/heights/dist_ph_along"][...]
                    segment_id = f[f"{beam}/geolocation/segment_id"][...]
                    seg_ph_cnt = f[f"{beam}/geolocation/segment_ph_cnt"][...]
                    seg_length = f[f"{beam}/geolocation/segment_length"][...]
                except KeyError:
                    continue

                n = len(dist_ph_along)
                seg_starts = np.concatenate(([0], np.cumsum(seg_ph_cnt)[:-1]))
                seg_cumul_start = np.concatenate(([0.0], np.cumsum(seg_length[:-1])))
                seg_of_ph = np.repeat(np.arange(len(seg_ph_cnt)), seg_ph_cnt)[:n]
                along = seg_cumul_start[seg_of_ph] + dist_ph_along[: len(seg_of_ph)]
                tables[beam] = (segment_id, seg_starts, along)
        return tables

    @staticmethod
    def _along_track_of_photons(df: pd.DataFrame, tables: dict) -> np.ndarray:
        """Look each photon's along-track distance up by its beam, segment and place in it.

        The lookup is by the photon's row in the file (``ph_segment_id`` and
        ``ph_index_within_seg``, which globato carries through), not by its
        coordinates: globato moves ATL24 bathymetry photons to ATL24's positions,
        so their coordinates no longer match the file. NaN where a photon's
        segment is not in the file's table.
        """
        out = np.full(len(df), np.nan)
        # globato yields the beam name as 4 bytes; a str is accepted too.
        lasers = np.char.decode(df["laser"].to_numpy().astype("S4"))
        seg = df["ph_segment_id"].to_numpy()
        idx = df["ph_index_within_seg"].to_numpy()
        for beam, (segment_id, seg_starts, along) in tables.items():
            sel = np.flatnonzero(lasers == beam)
            if len(sel) == 0 or len(segment_id) == 0:
                continue
            pos = np.clip(np.searchsorted(segment_id, seg[sel]), 0, len(segment_id) - 1)
            rows = seg_starts[pos] + idx[sel] - 1
            ok = (segment_id[pos] == seg[sel]) & (rows >= 0) & (rows < len(along))
            out[sel[ok]] = along[rows[ok]]
        return out

    @staticmethod
    def _validate_vertical_datum(raw_value: str) -> str:
        """Validate and normalize icesat2_vertical_datum to 'ellipsoid' or 'geoid'."""
        normalized = str(raw_value).strip().lower()
        if normalized not in ("ellipsoid", "geoid"):
            raise ValueError(
                f"Invalid icesat2_vertical_datum value: {raw_value!r}. "
                "Must be 'ellipsoid' or 'geoid' (case-insensitive).",
            )
        return normalized

    @staticmethod
    def _vertical_datum_to_vertical_epsg(vertical_datum: str) -> str:
        """Map a validated vertical_datum value to its vertical EPSG code string."""
        return "EPSG:4979" if vertical_datum == "ellipsoid" else "EPSG:3855"

    # Maps IVERT's internal vertical-datum EPSG codes to the literal strings
    # globato.read()'s vertical_datum kwarg accepts. globato silently falls back
    # to "ellipsoid" for any value it doesn't recognize (it does not raise), so
    # passing an EPSG code straight through would be a silent no-op rather than
    # an error -- always go through this lookup instead.
    _EPSG_TO_GLOBATO_VERTICAL_DATUM: ClassVar[dict[str, str]] = {
        "EPSG:4979": "ellipsoid",
        "EPSG:3855": "geoid",
    }

    # Kept separately from the mapping above so a divergence between the two
    # (e.g. if globato's ICESat2Reader changes its accepted vocabulary) is
    # caught explicitly rather than silently mis-mapped.
    _GLOBATO_ACCEPTED_VERTICAL_DATUMS = frozenset(
        {"ellipsoid", "ellipsoid-mean-tide", "geoid", "geoid-mean-tide"},
    )

    @classmethod
    def _vertical_epsg_to_globato_datum(cls, vertical_epsg: str) -> str:
        """Map an IVERT vertical-datum EPSG code to the literal globato.read() expects.

        Raises ValueError if the EPSG code has no known mapping, or if the mapped
        value isn't one globato's ICESat2Reader currently accepts.
        """
        try:
            globato_datum = cls._EPSG_TO_GLOBATO_VERTICAL_DATUM[vertical_epsg]
        except KeyError:
            raise ValueError(
                f"No globato vertical_datum mapping for {vertical_epsg!r}. "
                "Known mappings: "
                f"{cls._EPSG_TO_GLOBATO_VERTICAL_DATUM}.",
            ) from None

        if globato_datum not in cls._GLOBATO_ACCEPTED_VERTICAL_DATUMS:
            raise ValueError(
                f"Mapped globato vertical_datum {globato_datum!r} (from "
                f"{vertical_epsg!r}) is not one of globato's accepted values "
                f"{sorted(cls._GLOBATO_ACCEPTED_VERTICAL_DATUMS)}. globato's "
                "ICESat2Reader may have changed its accepted vertical_datum "
                "vocabulary; update _EPSG_TO_GLOBATO_VERTICAL_DATUM / "
                "_GLOBATO_ACCEPTED_VERTICAL_DATUMS to match.",
            )
        return globato_datum

    def _classify_h5(
        self,
        h5_fn: str,
        query_bbox: tuple,
        classes_to_keep: tuple = (1, 2, 3, 6, 7, 40, 41, 42),
        min_confidence_level: int = 1,
        use_external_masks: bool = True,
    ) -> tuple[pd.DataFrame, str] | None:
        """Classify an ATL03 HDF5 file with globato and return its photons over a box.

        This is the expensive step, so it is done once per granule and box; the
        result can then be cut into storage tiles without reading the file again.

        Returns (photons, vertical_datum), where photons holds only the classes in
        classes_to_keep with the columns the database stores, or None if no
        photons survived filtering.
        """
        vertical_datum = self._validate_vertical_datum(
            self.config.icesat2_vertical_datum,
        )
        vertical_datum = self._vertical_datum_to_vertical_epsg(vertical_datum)

        classes_str = "/".join([str(int(c)) for c in classes_to_keep])
        region_str = f"{query_bbox[0]}/{query_bbox[1]}/{query_bbox[2]}/{query_bbox[3]}"

        stream = globato.read(
            h5_fn,
            data_type="ATL03",
            region=region_str,
            classes=classes_str,
            vertical_datum=self._vertical_epsg_to_globato_datum(vertical_datum),
            reject_failed_qa=True,
            append_atl24=True,
            cache_dir=self.icesat2_download_dir,
            use_external_masks=use_external_masks,
        )

        chunks = [pd.DataFrame(chunk) for chunk in stream]

        if not chunks:
            return None

        df = pd.concat(chunks, ignore_index=True)
        df = df.rename(columns={"ph_h_classed": "class_code"})

        # Temporal filter
        if "delta_time" in df.columns:
            dt_min = _yyyymmdd_to_delta_time(query_bbox[4])
            dt_max = _yyyymmdd_to_delta_time(query_bbox[5])
            df = df[(df["delta_time"] >= dt_min) & (df["delta_time"] < dt_max)]

        if len(df) == 0:
            return None

        if min_confidence_level > 1 and "confidence" in df.columns:
            df = df[df["confidence"] >= min_confidence_level]
            if len(df) == 0:
                return None

        # Each photon's cumulative along-track distance, looked up by its row in
        # the file (see _along_track_of_photons).
        if {"laser", "ph_segment_id", "ph_index_within_seg"} <= set(df.columns):
            beams = sorted(set(np.char.decode(df["laser"].to_numpy().astype("S4"))))
            tables = self._h5_along_track_m(h5_fn, beams)
            df = df.assign(along_track_m=self._along_track_of_photons(df, tables))

        # Keep only the columns needed for validation; drop large/redundant ones.
        keep_cols = [
            "x",
            "y",
            "z",
            "class_code",
            "bathy_confidence",
            "delta_time",
            "confidence",
            "laser",
            "along_track_m",
        ]
        df = df[[c for c in keep_cols if c in df.columns]].copy()

        return df, vertical_datum

    @classmethod
    def _granule_attrs(
        cls,
        df: pd.DataFrame,
        nc_fn: str,
        query_bbox: tuple,
        base_attrs: dict,
    ) -> dict:
        """The global attributes of a granule file holding the photons in df.

        Everything that describes the photons themselves (data_bbox, zbounds, the
        per-class counts) is computed from df. base_attrs supplies the rest:
        'source_granule', 'downloaded_on_utc', 'horizontal_datum' and
        'vertical_datum', and optionally 'laser_name'.
        """
        xmin, xmax = float(df["x"].min()), float(df["x"].max())
        ymin, ymax = float(df["y"].min()), float(df["y"].max())
        zmin = float(df["z"].min()) if "z" in df.columns else float("nan")
        zmax = float(df["z"].max()) if "z" in df.columns else float("nan")
        if "delta_time" in df.columns:
            tmin = int(_delta_time_to_yyyymmdd(float(df["delta_time"].min())))
            tmax = int(_delta_time_to_yyyymmdd(float(df["delta_time"].max())))
        else:
            tmin, tmax = int(query_bbox[4]), int(query_bbox[5])

        cc = df["class_code"]
        return {
            "granule_id": os.path.splitext(os.path.basename(nc_fn))[0],
            "source_granule": str(base_attrs["source_granule"]),
            "laser_name": str(base_attrs.get("laser_name", "all")),
            "query_bbox": [float(v) for v in query_bbox[:4]]
            + [int(v) for v in query_bbox[4:]],
            "data_bbox": [xmin, xmax, ymin, ymax, tmin, tmax],
            "zbounds": [zmin, zmax],
            "numphotons": len(df),
            "numphotons_unclassified": int(np.count_nonzero(cc == -1)),
            "numphotons_noise": int(np.count_nonzero(cc == 0)),
            "numphotons_ground": int(np.count_nonzero(cc == 1)),
            "numphotons_canopy": int(np.count_nonzero(cc == 2)),
            "numphotons_canopy_top": int(np.count_nonzero(cc == 3)),
            "numphotons_ice_surface": int(np.count_nonzero(cc == 6)),
            "numphotons_buildings": int(np.count_nonzero(cc == 7)),
            "numphotons_bathy_floor": int(np.count_nonzero(cc == 40)),
            "numphotons_bathy_surface": int(np.count_nonzero(cc == 41)),
            "numphotons_inland_water_surface": int(np.count_nonzero(cc == 42)),
            "downloaded_on_utc": int(base_attrs["downloaded_on_utc"]),
            "horizontal_datum": str(base_attrs["horizontal_datum"]),
            "vertical_datum": str(base_attrs["vertical_datum"]),
        }

    @staticmethod
    def _save_nc(df: pd.DataFrame, nc_fn: str, attrs: dict) -> None:
        """Write photons and their global attributes to a NetCDF granule file."""
        xr_ds = xarray.Dataset.from_dataframe(df.reset_index(drop=True))
        xr_ds.attrs = attrs
        os.makedirs(os.path.dirname(nc_fn) or ".", exist_ok=True)
        xr_ds.to_netcdf(nc_fn)

    def _write_nc(
        self,
        df: pd.DataFrame,
        h5_fn: str,
        nc_fn: str,
        query_bbox: tuple,
        vertical_datum: str,
        progress: str = "",
    ) -> dict:
        """Save classified photons as a NetCDF granule file and return its index record.

        The file carries rich metadata attributes so the database can be rebuilt
        from headers alone.
        """
        metadata_attrs = self._granule_attrs(
            df,
            nc_fn,
            query_bbox,
            {
                "source_granule": self._granule_stem(h5_fn),
                "downloaded_on_utc": datetime.datetime.now(datetime.UTC).strftime(
                    "%Y%m%d",
                ),
                "horizontal_datum": "EPSG:4326",
                "vertical_datum": vertical_datum,
            },
        )
        self._save_nc(df, nc_fn, metadata_attrs)
        logger.info(
            "%sSaved %s (%s photons, %s ground, %s bathy).",
            progress,
            os.path.basename(nc_fn),
            f"{metadata_attrs['numphotons']:,}",
            f"{metadata_attrs['numphotons_ground']:,}",
            f"{metadata_attrs['numphotons_bathy_floor']:,}",
        )

        return self._index_record_from_attrs(
            metadata_attrs,
            os.path.basename(nc_fn),
        )

    @classmethod
    def clip_granule_file(cls, nc_fn: str, cuboids, out_dir: str) -> list[dict]:
        """Write the photons of a granule file that fall in each cuboid to a file of its own.

        Each (xmin, xmax, ymin, ymax, tmin, tmax) cuboid, with tmin/tmax as YYYYMMDD,
        becomes the query_bbox of a new file in out_dir, named like any granule file
        for that box. Boxes are half-open, as in read_granule(): x < xmax, y < ymax,
        and dates before tmax, so a photon on a shared edge goes to exactly one
        piece. Every column is kept. The new files keep the original's source
        granule, download date and datums; their photon bounds and per-class counts
        are recomputed. A cuboid holding no photons writes no file.

        Returns:
            The index records (see _index_record_from_attrs) of the files written.

        """
        with xarray.open_dataset(nc_fn) as ds:
            df = ds.to_dataframe().reset_index(drop=True)
            attrs = dict(ds.attrs)

        base_attrs = {
            "source_granule": attrs.get(
                "source_granule",
                cls._source_granule_from_filename(os.path.basename(nc_fn)),
            ),
            "laser_name": attrs.get("laser_name", "all"),
            "downloaded_on_utc": attrs.get(
                cls._stored_col_name("downloaded_on_utc", attrs),
                0,
            ),
            "horizontal_datum": attrs.get("horizontal_datum", "EPSG:4326"),
            "vertical_datum": attrs.get("vertical_datum", "EPSG:4979"),
        }

        records = []
        for cuboid in cuboids:
            piece = cls._photons_in_bbox(df, cuboid)
            if "delta_time" in piece.columns:
                dt = piece["delta_time"]
                piece = piece[
                    (dt >= _yyyymmdd_to_delta_time(cuboid[4]))
                    & (dt < _yyyymmdd_to_delta_time(cuboid[5]))
                ]
            if len(piece) == 0:
                continue
            out_fn = os.path.join(
                out_dir,
                cls._granule_stem(nc_fn) + cls._query_bbox_suffix(cuboid) + ".nc",
            )
            piece_attrs = cls._granule_attrs(piece, out_fn, cuboid, base_attrs)
            cls._save_nc(piece, out_fn, piece_attrs)
            records.append(
                cls._index_record_from_attrs(piece_attrs, os.path.basename(out_fn)),
            )
        return records

    @staticmethod
    def _photons_in_bbox(df: pd.DataFrame, bbox: tuple) -> pd.DataFrame:
        """Return the photons inside a box, with the same edge rule read_granule() uses."""
        return df[
            (df["x"] >= bbox[0])
            & (df["x"] < bbox[1])
            & (df["y"] >= bbox[2])
            & (df["y"] < bbox[3])
        ]

    def _process_h5_to_nc_tiles(
        self,
        h5_fn: str,
        query_bbox: tuple,
        tiles: list,
        classes_to_keep: tuple = (1, 2, 3, 6, 7, 40, 41, 42),
        overwrite: bool = False,
        min_confidence_level: int = 1,
        granule_num: int | None = None,
        total_granules: int | None = None,
        use_external_masks: bool = True,
    ) -> list[dict]:
        """Classify one downloaded subset and store it as one .nc file per storage tile.

        The subset was cut by Harmony to ``query_bbox``; it is classified once, and
        its photons are then dealt out to ``tiles``, a list of (tile_bbox, nc_fn)
        pairs that partition that box. A tile that already has its file keeps it
        unless ``overwrite`` is set, and a tile that receives no photons gets no
        file.

        Returns the index records of the tiles that have a file.
        """
        records = []
        to_write = []
        for tile_bbox, nc_fn in tiles:
            if os.path.exists(nc_fn) and not overwrite:
                meta = self._read_nc_metadata(nc_fn)
                if meta is not None:
                    records.append(meta)
            else:
                to_write.append((tile_bbox, nc_fn))

        if not to_write:
            return records

        classified = self._classify_h5(
            h5_fn,
            query_bbox,
            classes_to_keep=classes_to_keep,
            min_confidence_level=min_confidence_level,
            use_external_masks=use_external_masks,
        )
        if classified is None:
            return records
        df, vertical_datum = classified

        progress = (
            f"{granule_num}/{total_granules} "
            if granule_num is not None and total_granules is not None
            else ""
        )
        for tile_bbox, nc_fn in to_write:
            tile_df = self._photons_in_bbox(df, tile_bbox)
            if len(tile_df) == 0:
                continue
            records.append(
                self._write_nc(
                    tile_df,
                    h5_fn,
                    nc_fn,
                    tile_bbox,
                    vertical_datum,
                    progress=progress,
                ),
            )
        return records

    def _start_aux_prefetch(self, h5_files: list):
        """Start fetching the subsets' ATL08 and ATL24 granules in a child process.

        Returns ``(child, ready)``: the child process to join, and a queue on
        which it reports each subset's path once that subset's files have been
        looked for, then ``None``. Returns ``None`` where fork is unavailable, in
        which case the classifiers fetch for themselves as before. The fetching
        happens in a forked child rather than in threads here, because the
        classification pool forks this process too, and forking with live
        threads is unsafe.
        """
        if not h5_files or not (
            sys.platform.startswith("linux")
            and "fork" in multiprocessing.get_all_start_methods()
        ):
            return None
        context = multiprocessing.get_context("fork")
        ready = context.Queue()
        child = context.Process(
            target=_prefetch_aux_granules,
            args=(
                list(h5_files),
                self.icesat2_download_dir,
                _AUX_PREFETCH_THREADS,
                ready,
            ),
            daemon=True,
        )
        child.start()
        return child, ready

    @staticmethod
    def _as_ready(h5_files: list, prefetch) -> Iterator[str]:
        """Yield the subsets in the order their aux granules become available.

        With no prefetch, that is the given order. Otherwise each subset is
        yielded when the prefetch child reports it, and whatever it has not
        reported when it finishes (or dies) is yielded in the given order, so a
        prefetch failure costs downloads, not granules.
        """
        if prefetch is None:
            yield from h5_files
            return
        child, ready = prefetch
        pending = list(h5_files)
        while pending:
            try:
                item = ready.get(timeout=_READY_POLL_SECONDS)
            except queue.Empty:
                if child.is_alive():
                    continue
                logger.warning(
                    "The aux prefetch stopped early; the remaining %d granules "
                    "fetch their own ATL08/ATL24.",
                    len(pending),
                )
                break
            if item is None:
                break
            if item in pending:
                pending.remove(item)
                yield item
        yield from pending

    def _classify_files(
        self,
        files_to_process: list,
        query_bbox: tuple,
        classes_to_keep: tuple = (1, 2, 3, 6, 7, 40, 41, 42),
        min_confidence_level: int = 1,
        use_external_masks: bool = True,
    ) -> list[dict]:
        """Classify each downloaded subset and store its tiles, in parallel when the machine allows.

        ``files_to_process`` holds ``(h5_fn, tiles)`` pairs as
        :meth:`_process_h5_to_nc_tiles` takes them. The subsets are taken largest
        first. Their ATL08 and ATL24 granules are fetched ahead by a child process
        (see :meth:`_start_aux_prefetch`), and each subset is classified once its
        files are in: the first in this process, which also leaves globato's mask
        trees built here for the workers to inherit when they fork, and the rest
        by a pool sized by :func:`classify_worker_count`, or here one after
        another when that size is 1. So only the prefetch downloads, and its
        progress bars are the only ones on the screen. Granules that produce no
        photons are logged.

        Returns the index records of every tile written.
        """
        total = len(files_to_process)
        ordered = sorted(
            files_to_process,
            key=lambda item: os.path.getsize(item[0]),
            reverse=True,
        )
        tiles_of = dict(ordered)
        records = []
        if not ordered:
            return records

        def job(index, h5_fn):
            return {
                "h5_fn": h5_fn,
                "query_bbox": query_bbox,
                "tiles": tiles_of[h5_fn],
                "classes_to_keep": classes_to_keep,
                "min_confidence_level": min_confidence_level,
                "granule_num": index,
                "total_granules": total,
                "use_external_masks": use_external_masks,
            }

        def take(index, h5_fn, metas):
            if metas:
                records.extend(metas)
            else:
                logger.info(
                    "%d/%d No valid classified photons in %s.",
                    index,
                    total,
                    os.path.basename(h5_fn),
                )

        h5_files = [h5_fn for h5_fn, _ in ordered]
        prefetch = self._start_aux_prefetch(h5_files)
        try:
            ready = self._as_ready(h5_files, prefetch)
            first = next(ready)
            take(1, first, self._process_h5_to_nc_tiles(**job(1, first)))
            if total == 1:
                return records

            workers, why = classify_worker_count(
                getattr(self.config, "icesat2_classify_workers", "auto"),
                h5_files[1:],
            )
            workers = min(workers, total - 1)
            numbered = ((index, h5_fn) for index, h5_fn in enumerate(ready, start=2))
            if workers <= 1:
                for index, h5_fn in numbered:
                    take(
                        index,
                        h5_fn,
                        self._process_h5_to_nc_tiles(**job(index, h5_fn)),
                    )
                return records

            logger.info(
                "Classifying the remaining %d granules with %d worker processes (%s).",
                total - 1,
                workers,
                why,
            )
            jobs = ((index, job(index, h5_fn)) for index, h5_fn in numbered)
            pool = multiprocessing.get_context("fork").Pool(
                workers,
                initializer=_start_classify_worker,
                initargs=(self.config, _CLASSIFY_NICE),
            )
            try:
                for index, h5_fn, metas in pool.imap_unordered(
                    _classify_in_worker,
                    jobs,
                ):
                    take(index, h5_fn, metas)
                pool.close()
            except BaseException:
                pool.terminate()
                raise
            finally:
                pool.join()
            return records
        finally:
            if prefetch is not None:
                prefetch[0].join()

    def _process_h5_to_nc(
        self,
        h5_fn: str,
        nc_fn: str,
        query_bbox: tuple,
        classes_to_keep: tuple = (1, 2, 3, 6, 7, 40, 41, 42),
        overwrite: bool = False,
        min_confidence_level: int = 1,
        granule_num: int | None = None,
        total_granules: int | None = None,
        use_external_masks: bool = True,
    ) -> dict | None:
        """Classify an ATL03 HDF5 file with globato and save the result as one NetCDF file.

        The single-tile form of :meth:`_process_h5_to_nc_tiles`: the whole box goes
        into one file. Returns its index record, or None if no photons survived.
        """
        records = self._process_h5_to_nc_tiles(
            h5_fn,
            query_bbox,
            [(tuple(query_bbox), nc_fn)],
            classes_to_keep=classes_to_keep,
            overwrite=overwrite,
            min_confidence_level=min_confidence_level,
            granule_num=granule_num,
            total_granules=total_granules,
            use_external_masks=use_external_masks,
        )
        return records[0] if records else None

    def _write_index(self, df) -> None:
        """Serialize the index DataFrame to the single NetCDF index file.

        Every column is written as a plain 1-D variable over the "record"
        dimension (strings as object arrays, everything else as int64/float64),
        and the numeric variables are zlib-compressed. No geometry is stored: the
        footprint is always box(data_bbox) and is rebuilt from the data_bbox_*
        columns if ever needed. This layout lets the index read straight back
        into a DataFrame with no shapely reconstruction.
        """
        n = len(df)
        data_vars = {}

        for col in self._INDEX_STR_COLS:
            values = (
                np.array(
                    ["" if v is None else str(v) for v in df[col]],
                    dtype=object,
                )
                if n
                else np.array([], dtype=object)
            )
            data_vars[col] = ("record", values)

        int_cols = (*self._INDEX_INT_COLS, *self._INDEX_BBOX_INT_COLS)
        for col in int_cols:
            values = (
                np.asarray(df[col].to_list(), dtype="int64")
                if n
                else np.array([], dtype="int64")
            )
            data_vars[col] = ("record", values)

        float_cols = (*self._INDEX_BBOX_FLOAT_COLS, *self._INDEX_ZBOUNDS_COLS)
        for col in float_cols:
            values = (
                np.asarray(df[col].to_list(), dtype="float64")
                if n
                else np.array([], dtype="float64")
            )
            data_vars[col] = ("record", values)

        ds = xarray.Dataset(data_vars)
        ds.attrs["crs"] = self.crs

        # Compress the numeric variables. Variable-length string variables can't
        # take zlib under the netCDF4 engine, and there is nothing to chunk when
        # the index is empty.
        encoding = {}
        if n:
            for col in (*int_cols, *float_cols):
                encoding[col] = {"zlib": True, "complevel": 4}

        os.makedirs(
            os.path.dirname(self.db_fname) or ".",
            exist_ok=True,
        )
        ds.to_netcdf(self.db_fname, encoding=encoding)

    @classmethod
    def read_index_file(cls, index_fname: str) -> pd.DataFrame:
        """Read any IVERT NetCDF index file into a plain pandas DataFrame.

        Every column comes back as a numpy array with no per-row Python loops and
        no geometry reconstruction, which is what makes this read fast; spatial
        filtering is done downstream with vectorized bbox comparisons on the
        data_bbox_* / query_bbox_* columns. Columns are in the canonical order
        given by _empty_db_dict().
        """
        with xarray.open_dataset(index_fname) as ds_on_disk:
            ds = ds_on_disk.load()

        data = {}
        for col in cls._INDEX_STR_COLS:
            data[col] = ds[col].to_numpy().astype(str)
        for col in (
            *cls._INDEX_INT_COLS,
            *cls._INDEX_BBOX_INT_COLS,
            *cls._INDEX_BBOX_FLOAT_COLS,
            *cls._INDEX_ZBOUNDS_COLS,
        ):
            data[col] = ds[cls._stored_col_name(col, ds)].to_numpy()

        df = pd.DataFrame(data)
        # Restore the canonical column order used elsewhere in the codebase.
        return df[list(cls._empty_db_dict().keys())]

    def _read_index(self):
        """Read this database's index file back into a plain pandas DataFrame.

        Returns None if the index file does not exist.
        """
        if not os.path.exists(self.db_fname):
            return None

        return self.read_index_file(self.db_fname)

    def open_gdf(
        self,
        force_reread: bool = False,
    ):
        """Get the index DataFrame from the database.

        Args:
            force_reread: If True, read the file again even if we've already read the database into memory.

        Returns:
            pandas.DataFrame of the granule records in the database (bounding boxes
            held as the scalar query_bbox_* / data_bbox_* columns; no geometry).
            None if no current database file exists locally.

        """
        if self.gdf is not None and not force_reread:
            return self.gdf

        gdf = self._read_index()
        if gdf is None:
            return None

        self.gdf = gdf
        logger.info(
            "Loaded %s with %d records.",
            os.path.basename(self.db_fname),
            len(self.gdf),
        )

        return self.gdf

    def read_database_file(
        self,
        bbox: list | tuple | None = None,
        date_range: list | tuple | None = None,
    ):
        """Read the master database into a DataFrame.

        Subset list of granules by bounding box and date range of the data (not the query box).

        Return the subset of the database read off of disk.
        """
        gdf_subset = self._read_index()
        if gdf_subset is None:
            return None

        if bbox is not None:
            # bbox is (xmin, ymin, xmax, ymax); keep granules whose data bbox overlaps.
            xmin, ymin, xmax, ymax = bbox
            gdf_subset = gdf_subset[
                (gdf_subset["data_bbox_xmin"] <= xmax)
                & (gdf_subset["data_bbox_xmax"] >= xmin)
                & (gdf_subset["data_bbox_ymin"] <= ymax)
                & (gdf_subset["data_bbox_ymax"] >= ymin)
            ]

        if date_range is not None:
            date_range = self.convert_date_range(date_range)
            gdf_subset = gdf_subset[
                (gdf_subset["data_bbox_tmin"] >= date_range[0])
                & (gdf_subset["data_bbox_tmax"] <= date_range[1])
            ]

        self.last_gdf_date_range = date_range
        self.last_gdf_bbox = tuple(bbox) if bbox is not None else None

        return gdf_subset

    @staticmethod
    def omit_photons_from_exclusion_bbox(
        dataframe,
        bbox_to_exclude,
    ) -> pd.DataFrame:
        """Exclude any photons that fall within the particular bounding box."""
        x = dataframe["x"]
        y = dataframe["y"]
        dt = dataframe["delta_time"]

        if len(bbox_to_exclude) == 6:
            bbox_dt_min = _yyyymmdd_to_delta_time(bbox_to_exclude[4])
            bbox_dt_max = _yyyymmdd_to_delta_time(bbox_to_exclude[5])
            df_sub = dataframe[
                (x < bbox_to_exclude[0])
                | (x >= bbox_to_exclude[1])
                | (y < bbox_to_exclude[2])
                | (y >= bbox_to_exclude[3])
                | (dt < bbox_dt_min)
                | (dt >= bbox_dt_max)
            ]

        elif len(bbox_to_exclude) == 4:
            df_sub = dataframe[
                (x < bbox_to_exclude[0])
                | (x >= bbox_to_exclude[1])
                | (y < bbox_to_exclude[2])
                | (y >= bbox_to_exclude[3])
            ]

        else:
            raise ValueError(
                "Bounding boxes must be either 4 values or 6 values, in format (xmin, xmax, ymin, ymax, [tmin, tmax]).",
            )

        return df_sub

    @staticmethod
    def read_granule(
        granule_fn: str,
        subset_bbox: list | tuple | None = None,
        photon_classes: list | tuple | None = None,
    ) -> pd.DataFrame:
        """Read classified photons from a NetCDF granule file.

        Args:
            granule_fn: Path to a processed .nc granule file in the granules directory.
            subset_bbox: 6-value bounding box (xmin, xmax, ymin, ymax, tmin, tmax) where t is YYYYMMDD.
            photon_classes: Photon class codes to return. Defaults to (1, 40) (ground and bathy floor).

        Returns:
            pandas.DataFrame with columns x, y, z, class_code, bathy_confidence, delta_time.

        """
        if photon_classes is None:
            photon_classes = (1, 40)

        ds = xarray.open_dataset(granule_fn)
        df = ds.to_dataframe().reset_index(drop=True)
        ds.close()

        # Filter by photon class.
        df = df[df["class_code"].isin(photon_classes)]

        if subset_bbox is not None:
            assert len(subset_bbox) == 6, (
                "subset_bbox must have 6 values (xmin, xmax, ymin, ymax, tmin, tmax)."
            )
            x, y = df["x"], df["y"]
            df = df[
                (x >= subset_bbox[0])
                & (x < subset_bbox[1])
                & (y >= subset_bbox[2])
                & (y < subset_bbox[3])
            ]

            if "delta_time" in df.columns:
                dt_min = _yyyymmdd_to_delta_time(subset_bbox[4])
                dt_max = _yyyymmdd_to_delta_time(subset_bbox[5])
                df = df[(df["delta_time"] >= dt_min) & (df["delta_time"] < dt_max)]

        return df

    @staticmethod
    def is_iterable(obj) -> bool:
        try:
            iter(obj)
        except TypeError:
            return False
        else:
            return True

    def query_photons(
        self,
        bbox: list | tuple | None = None,
        photon_classes: list | tuple | None = (1, 6, 40),
        min_bathy_confidence=0.75,
        min_confidence_level: int = 1,
        omit_bboxes=None,
        # download_new_data: bool = False,
    ) -> pd.DataFrame | None:
        """Query the database for photons in a given bounding box and date range.

        Args:
            bbox: Bounding box to limit the data to, in [xmin, xmax, ymin, ymax, tmin, tmax]. Must be in WGS84 (EPSG: 4326)
                coordinates, and yyyymmdd integers for the date. Date range is not inclusive of the max date.
            photon_classes: Photon classes to include in the query. See globato/streams/readers/icesat2.py for the full list.
                Defaults to (1, 6, 40) (ground, land_ice, and bathy_floor photons).
            min_bathy_confidence (float): The minimum ATL24 confidence for bathymetric (class 40) photons to include (0.0-1.0).
            min_confidence_level: The minimum ATL03 signal confidence level to include (1-4). 1 keeps all photons.
            omit_bboxes (list, tuple, or None): Bounding box(es) whose photons are dropped from the results. Accepts a
                single 4- or 6-value bbox, or a sequence of them. Defaults to None,
                which excludes nothing.
            # download_new_data : bool
            #     Whether to download new ICESat-2 data from NASA if the current database doesn't contain the entire bounding box.

        Returns:
            pandas.DataFrame containing classified photons that fit in the bounding box and date range.
            If no photons are found, return None.

        """
        assert len(bbox) == 6, (
            "bbox must be a list or tuple of length 6 (xmin, ymin, xmax, ymax, tmin, tmax)."
        )

        gdf_subset = self.query_granules(bbox)

        logger.info("Reading %d granules overlapping %r.", len(gdf_subset), bbox)

        # print(gdf_subset)
        fnames = gdf_subset["filename"].apply(
            lambda x: os.path.join(self.granules_dir, x),
        )
        logger.info(
            "%d granules exist with %s ground photons and %s bathy_floor photons.",
            np.count_nonzero(fnames.apply(os.path.exists)),
            f"{gdf_subset['numphotons_ground'].sum():,}",
            f"{gdf_subset['numphotons_bathy_floor'].sum():,}",
        )

        granule_dfs = []
        for _idx, granule_line in gdf_subset.iterrows():
            fpath = os.path.join(self.granules_dir, granule_line["filename"])
            # print(os.path.basename(fpath))
            granule_dfs.append(
                self.read_granule(
                    fpath,
                    subset_bbox=bbox,
                    photon_classes=photon_classes,
                ),
            )
            # print()

        if len(granule_dfs) == 0:
            return None

        photons_df = pd.concat(granule_dfs, ignore_index=True)

        if min_bathy_confidence > 0.0:
            photons_df = photons_df[
                (photons_df["class_code"] != 40)
                | (
                    (photons_df["class_code"] == 40)
                    & (photons_df["bathy_confidence"] >= min_bathy_confidence)
                )
            ]

        if min_confidence_level > 1 and "confidence" in photons_df.columns:
            photons_df = photons_df[photons_df["confidence"] >= min_confidence_level]

        if omit_bboxes is None:
            omit_bboxes = []

        # If we're given a single bounding box of exclusions as a 4- or 6-tuple of numbers (not iterables), put it in a 1-length list.
        if len(omit_bboxes) in (4, 6) and not np.any(
            [self.is_iterable(num) for num in omit_bboxes],
        ):
            omit_bboxes = [omit_bboxes]

        if len(omit_bboxes) >= 1:
            for omit_bb in omit_bboxes:
                photons_df = self.omit_photons_from_exclusion_bbox(photons_df, omit_bb)

        if len(photons_df) > 0:
            logger.info(
                "Trimmed granules from %s to %s photons (%s ground, %s bathy).",
                f"{gdf_subset['numphotons'].sum():,}",
                f"{len(photons_df):,}",
                f"{np.count_nonzero(photons_df['class_code'] == 1):,}",
                f"{np.count_nonzero(photons_df['class_code'] == 40):,}",
            )
        else:
            logger.info("No photons in bbox.")

        # all of this subsetting can create a fractured dataframe that is a subset-of-subset-of... iteration.
        # If we simply copy the dataframe upon returning it will be cleaner, without pointing to larger datasets and masks.
        return photons_df.copy()

    def convert_date_range(
        self,
        date_range: list | tuple | None,
    ) -> list | tuple | None:
        """Convert date range to the format required by the database."""
        if date_range is None:
            return None
        if len(date_range) == 2:
            return self.convert_date_to_yyyymmdd(
                date_range[0],
            ), self.convert_date_to_yyyymmdd(date_range[1])
        raise ValueError("Date range must be a list or tuple of length 2.")

    def convert_date_to_yyyymmdd(
        self,
        date: int | str | datetime.datetime | datetime.date,
    ) -> int:
        """Convert date to the YYYYMMDD integer format required by the database."""
        if isinstance(date, int):
            # If it's an integer, make sure it's 8 digits and then return as-is.
            if len(str(date)) != 8:
                raise ValueError("Date must be an 8 digit integer in YYYYMMDD.")
            return date
        if isinstance(date, str):
            try:
                # If it's a string in "YYYYMMDD" format, convert it to an int.
                date_int = int(date)
                return self.convert_date_to_yyyymmdd(date_int)
            except ValueError:
                # If it isn't a YYYYMMDD string, parse it with dateparser.
                return int(dateparser.parse(date).strftime("%Y%m%d"))
        elif isinstance(date, (datetime.datetime, datetime.date)):
            return int(date.strftime("%Y%m%d"))
        else:
            raise ValueError(
                "Date must be an int, str, datetime.datetime, or datetime.date.",
            )

    def query_granules(self, bbox: list | tuple) -> pd.DataFrame | None:
        """Return a sub-dataframe of granules in the database that possibly intersect the bounding box, using data bounding boxes."""
        gdf = self.open_gdf()
        if gdf is None or len(gdf) == 0:
            return None

        # To assess intersection, we must first increment the tmin of both the incoming bboxes and the query bbox by 1
        # to make it a non-inclusive limit.
        # query_bbox = tuple(bbox[0:5]) + (self.increment_yyyymmdd_by_n(bbox[5], 1),)

        bbox = (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
            int(bbox[4]),
            int(bbox[5]),
        )

        # Vectorized bbox-overlap test (x, y, and time) on the scalar data_bbox_*
        # columns — same semantics as the per-row cuboids_intersect it replaces.
        int_mask = ivert.utils.cuboid_funcs.cuboids_intersect_vectorized(
            gdf["data_bbox_xmin"].to_numpy(),
            gdf["data_bbox_xmax"].to_numpy(),
            gdf["data_bbox_ymin"].to_numpy(),
            gdf["data_bbox_ymax"].to_numpy(),
            gdf["data_bbox_tmin"].to_numpy(),
            gdf["data_bbox_tmax"].to_numpy(),
            bbox,
            bbox_order="axis",
        )

        # Return the subset of the dataframe of granules whose data bounding-box intersects the query bounding box.
        return gdf[int_mask]

    def get_photon_src_epsg(self) -> str:
        """Return the compound EPSG src string for photon coordinates stored in this database.

        Reads horizontal_datum and vertical_datum from the first database record and builds
        a compound string (e.g. 'EPSG:4326+3855' or 'EPSG:4326+4979').
        Falls back to 'EPSG:4326+3855' for databases created before datum fields were added.
        """
        gdf = self.open_gdf()
        if (
            gdf is not None
            and len(gdf) > 0
            and "horizontal_datum" in gdf.columns
            and "vertical_datum" in gdf.columns
        ):
            hd_vals = gdf["horizontal_datum"].dropna()
            hd_vals = hd_vals[hd_vals != ""]
            vd_vals = gdf["vertical_datum"].dropna()
            vd_vals = vd_vals[vd_vals != ""]
            if len(hd_vals) > 0 and len(vd_vals) > 0:
                hd = str(hd_vals.iloc[0])  # e.g. "EPSG:4326"
                vd = str(vd_vals.iloc[0])  # e.g. "EPSG:3855" or "EPSG:4979"
                vd_num = vd.rsplit(":", maxsplit=1)[-1]  # strip "EPSG:" prefix
                return f"{hd}+{vd_num}"  # e.g. "EPSG:4326+3855"
        return "EPSG:4326+4979"

    def download_new_granules(
        self,
        bbox: list | tuple,
        classes_to_keep=(1, 2, 3, 6, 7, 40, 41, 42),
        tile_size_deg=2.0,
        max_tile_scale_factor=1.5,
        min_bathy_confidence=0.01,
        min_confidence_level: int = 1,
        cache_subdir: str | None = None,
        replace: bool = False,
        geometry: shapely.Geometry | None = None,
    ) -> DownloadSummary:
        """Download ICESat-2 ATL03 granules from NASA using fetchez and register them in the database.

        Downloads raw HDF5 files into granules_dir; classification is deferred to read time via globato.
        Only downloads granules covering bboxes not already in the database, unless 'replace' is True,
        in which case the full requested bbox is (re-)downloaded and any existing granules it overlaps
        are replaced with the newly-downloaded data.

        The area is ``geometry`` (a shapely polygon or multipolygon in WGS84) if
        given, else the horizontal part of ``bbox``. Either way it is covered with as
        few rectangles as possible (2-degree squares over its bounding box, minus the
        squares it does not reach, merged; see :func:`tile_geometry_into_bboxes`), and
        each rectangle is one request to fetchez, since one large subset of a granule
        costs Harmony less than several small ones. The time range always comes
        from ``bbox``.

        Storage goes the other way: each downloaded subset is classified once and
        then stored as one .nc file per tile of roughly ``tile_size_deg`` degrees
        (see :func:`split_bbox_into_parts`; ``max_tile_scale_factor`` bounds how far
        an edge tile may stretch rather than leave a sliver), because a query reads
        every file that touches it whole, and small files make small queries cheap.

        Returns a :class:`DownloadSummary` counting how each sub-region of the request
        turned out, so callers can tell a download that failed from one that succeeded
        and found no data. Errors are logged and counted rather than raised: one
        unreachable sub-region does not abandon the rest of the request.
        """
        # Validate the configured water surface and derive the target vertical datum.
        vertical_datum_cfg = self._validate_vertical_datum(
            self.config.icesat2_vertical_datum,
        )
        target_vd = self._vertical_datum_to_vertical_epsg(vertical_datum_cfg)

        # Reject the download if existing records use a different datum.
        existing_gdf_check = self.open_gdf()
        if (
            existing_gdf_check is not None
            and len(existing_gdf_check) > 0
            and "vertical_datum" in existing_gdf_check.columns
        ):
            existing_vd_vals = existing_gdf_check["vertical_datum"].dropna()
            existing_vd_vals = existing_vd_vals[existing_vd_vals != ""]
            if len(existing_vd_vals) > 0:
                existing_vd = str(existing_vd_vals.iloc[0])
                existing_hd = "EPSG:4326"
                if "horizontal_datum" in existing_gdf_check.columns:
                    existing_hd_vals = existing_gdf_check["horizontal_datum"].dropna()
                    existing_hd_vals = existing_hd_vals[existing_hd_vals != ""]
                    if len(existing_hd_vals) > 0:
                        existing_hd = str(existing_hd_vals.iloc[0])
                if existing_vd != target_vd:
                    logger.error(
                        "Datum mismatch: the existing database stores data in "
                        "horizontal datum %s and vertical datum %s, but the current "
                        "configuration requests vertical datum %s "
                        "(icesat2_vertical_datum=%r). All granules in a single database "
                        "must share the same datum. Change 'icesat2_vertical_datum' in "
                        "your user config to match the existing database, or create a "
                        "new database.",
                        existing_hd,
                        existing_vd,
                        target_vd,
                        vertical_datum_cfg,
                    )
                    return DownloadSummary(parts_failed=1)

        # The regions actually asked for: the fewest rectangles that cover the
        # area, whether it came in as a geometry or as a plain box.
        tmin, tmax = int(bbox[4]), int(bbox[5])
        if geometry is None:
            geometry = shapely.box(bbox[0], bbox[2], bbox[1], bbox[3])
        requested = [
            (*tile, tmin, tmax) for tile in tile_geometry_into_bboxes(geometry)
        ]
        if len(requested) == 0:
            logger.info(
                "The requested area has no extent. Nothing to download.",
            )
            return DownloadSummary()
        logger.info(
            "The requested area is covered by %d rectangular query region(s).",
            len(requested),
        )

        if replace:
            bboxes = requested
            logger.info(
                "--replace enabled: re-downloading the full requested region and "
                "overwriting any overlapping existing granules.",
            )
        else:
            bboxes = self.filter_query_bboxes(requested)

            if len(bboxes) == 0:
                logger.info(
                    "All required granules already exist in the database. Nothing new to download.",
                )
                return DownloadSummary()

            def _as_floats(boxes):
                return sorted(tuple(float(v) for v in b) for b in boxes)

            if _as_floats(bboxes) != _as_floats(requested):
                logger.info(
                    "Existing database coverage partially overlaps the requested region. "
                    "Downloading only the missing sub-region(s) (%d area(s) to fill).",
                    len(bboxes),
                )

        actual_bbox = (
            min(bb[0] for bb in bboxes),
            max(bb[1] for bb in bboxes),
            min(bb[2] for bb in bboxes),
            max(bb[3] for bb in bboxes),
            int(min(bb[4] for bb in bboxes)),
            int(max(bb[5] for bb in bboxes)),
        )
        logger.info(
            "Downloading granules over %s in %d parts.",
            actual_bbox,
            len(bboxes),
        )

        os.makedirs(self.granules_dir, exist_ok=True)

        parts_downloaded = parts_empty = parts_failed = granules_added = 0

        for i, sbbox in enumerate(bboxes):
            logger.info("=" * 85)
            logger.info("Part %d of %d: %s", i + 1, len(bboxes), sbbox)
            logger.info("=" * 85)

            cache_dir = (
                os.path.join(self.icesat2_download_dir, cache_subdir)
                if cache_subdir is not None
                else self.icesat2_download_dir
            )
            os.makedirs(cache_dir, exist_ok=True)

            # fetchez region is "xmin/xmax/ymin/ymax"
            region_str = f"{sbbox[0]}/{sbbox[1]}/{sbbox[2]}/{sbbox[3]}"
            time_start = (
                datetime.datetime.strptime(str(int(sbbox[4])), "%Y%m%d")
                .replace(tzinfo=datetime.UTC)
                .strftime("%Y-%m-%dT00:00:00")
            )
            time_end = (
                datetime.datetime.strptime(str(int(sbbox[5])), "%Y%m%d")
                .replace(tzinfo=datetime.UTC)
                .strftime("%Y-%m-%dT00:00:00")
            )

            logger.info(
                "Fetching ATL03 granules: region=%s  %s -> %s",
                region_str,
                time_start,
                time_end,
            )
            src_region = fetchez.spatial.parse_region(region_str)[0]
            mod = _FetchezIceSat2(
                src_region=src_region,
                outdir=cache_dir,
                subset=True,
                time_start=time_start,
                time_end=time_end,
            )

            # Check for a cached Harmony job for this bbox before submitting.
            requests_csv = ICESat2RequestsCSV()
            cached = requests_csv.find_matching_request(
                "ATL03",
                sbbox,
                only_unexpired=True,
            )
            if cached:
                # Ping Harmony to verify the cached job completed without errors.
                # Jobs with "complete_with_errors", "failed", or "canceled" status
                # cannot be reliably re-used, so a new job must be submitted instead.
                _error_states = {"complete_with_errors", "failed", "canceled"}
                cached_job_id = cached["jobID"]
                current_status = mod.harmony_ping_for_status(cached_job_id)
                current_state = (
                    current_status.get("status", "") if current_status else ""
                )
                if current_state in _error_states:
                    logger.warning(
                        "Cached Harmony job %s has status '%s'; submitting a new job.",
                        cached_job_id,
                        current_state,
                    )
                    cached = None
                else:
                    mod.subset_job_id = cached_job_id
                    n_granules = cached.get("numInputGranules", "?")
                    logger.info(
                        "Re-using cached Harmony job (%s ATL03 granules): "
                        "https://harmony.earthdata.nasa.gov/jobs/%s",
                        n_granules,
                        mod.subset_job_id,
                    )
            if not cached:
                harmony_status = mod.harmony_make_request()
                if harmony_status and "jobID" in harmony_status:
                    mod.subset_job_id = harmony_status["jobID"]
                    n_granules = harmony_status.get("numInputGranules", "?")
                    logger.info(
                        "Harmony job submitted (%s ATL03 granules): "
                        "https://harmony.earthdata.nasa.gov/jobs/%s",
                        n_granules,
                        mod.subset_job_id,
                    )
                    requests_csv.add_record("ATL03", sbbox, harmony_status)

            mod.run()

            # Give each subset a name that says which box it was cut to, before
            # anything is fetched; see _subset_cache_filename for why.
            for entry in mod.results:
                dst = entry.get("dst_fn")
                if dst:
                    entry["dst_fn"] = os.path.join(
                        os.path.dirname(dst),
                        self._subset_cache_filename(dst, sbbox),
                    )

            # Update the CSV with the final status (links, progress=100, etc.)
            if mod.subset_job_id:
                final_status = mod.harmony_ping_for_status(mod.subset_job_id)
                if final_status:
                    requests_csv.update_record(
                        "ATL03",
                        sbbox,
                        final_status,
                        fail_quietly=True,
                    )

            results = fetchez.core.run_fetchez([mod])
            if not results:
                logger.warning(
                    "Harmony request returned no results for bbox %s. Skipping for now. "
                    "This may be because zero files were returned or Harmony is temporarily down. "
                    "You may re-run the command later if you feel this was in error.",
                    sbbox,
                )
                parts_failed += 1
                continue
            h5_files = sorted(
                os.path.abspath(entry["dst_fn"])
                for _, entry in results
                if entry.get("status") == 0
                and entry.get("dst_fn")
                and os.path.exists(entry["dst_fn"])
            )

            if not h5_files:
                logger.info("No granules downloaded for this bbox.")
                parts_empty += 1
                continue

            logger.info(
                "Downloaded %d ATL03 granule(s). Classifying and saving as NetCDF...",
                len(h5_files),
            )

            use_external_masks = True

            existing_gdf = self.open_gdf()
            existing_filenames = (
                set(existing_gdf["filename"].values)
                if existing_gdf is not None
                else set()
            )

            # One request, many files: the subset is stored per storage tile.
            storage_tiles = split_bbox_into_parts(
                sbbox,
                tile_size_deg=tile_size_deg,
                max_tile_scale_factor=max_tile_scale_factor,
            )

            files_to_process = []
            for h5_src in h5_files:
                targets = []
                for tile in storage_tiles:
                    nc_basename = self._nc_filename(h5_src, tile)
                    if nc_basename in existing_filenames and not replace:
                        logger.info("Skipping %s (already in database).", nc_basename)
                    else:
                        targets.append(
                            (tile, os.path.join(self.granules_dir, nc_basename)),
                        )
                if targets:
                    files_to_process.append((h5_src, targets))

            new_records = self._classify_files(
                files_to_process,
                sbbox,
                classes_to_keep=classes_to_keep,
                min_confidence_level=min_confidence_level,
                use_external_masks=use_external_masks,
            )

            # Keep the landmask globato just fetched for this part, one file per
            # storage tile, so validations here need not query OSM for it again.
            self._store_landmasks(storage_tiles)

            if not new_records:
                parts_empty += 1
                continue

            new_gdf = pd.DataFrame(new_records)[list(self._empty_db_dict().keys())]
            if existing_gdf is None or len(existing_gdf) == 0:
                self.gdf = new_gdf
            else:
                if replace:
                    # Match on the underlying NASA granule id (source_granule), not the full
                    # nc filename: the nc filename embeds the query bbox/dates, which can
                    # differ between the original download and this --replace re-download,
                    # so a filename-only comparison would fail to find the old record to drop.
                    new_source_granules = set(new_gdf["source_granule"].values)
                    if "source_granule" in existing_gdf.columns:
                        existing_source_granules = existing_gdf[
                            "source_granule"
                        ].fillna(
                            existing_gdf["filename"].apply(
                                self._source_granule_from_filename,
                            ),
                        )
                    else:
                        existing_source_granules = existing_gdf["filename"].apply(
                            self._source_granule_from_filename,
                        )
                    same_granule = existing_source_granules.isin(new_source_granules)
                    # Only replace records whose query bbox (lat/lon + time) overlaps the
                    # region/dates just (re-)downloaded (sbbox). Records for the same source
                    # granule but a non-overlapping query bbox are legitimately distinct data
                    # (e.g. a different date range or region) and must not be dropped, since
                    # doing so would discard original data rather than de-duplicate it.
                    bbox_overlaps = pd.Series(
                        ivert.utils.cuboid_funcs.cuboids_intersect_vectorized(
                            existing_gdf["query_bbox_xmin"].to_numpy(),
                            existing_gdf["query_bbox_xmax"].to_numpy(),
                            existing_gdf["query_bbox_ymin"].to_numpy(),
                            existing_gdf["query_bbox_ymax"].to_numpy(),
                            existing_gdf["query_bbox_tmin"].to_numpy(),
                            existing_gdf["query_bbox_tmax"].to_numpy(),
                            sbbox,
                            bbox_order="axis",
                        ),
                        index=existing_gdf.index,
                    )
                    is_replaced = same_granule & bbox_overlaps
                    n_replaced = int(is_replaced.sum())
                    if n_replaced:
                        logger.info(
                            "Replacing %d existing record(s) with newly-downloaded data.",
                            n_replaced,
                        )
                        for old_fname in existing_gdf.loc[is_replaced, "filename"]:
                            old_fpath = os.path.join(self.granules_dir, old_fname)
                            if os.path.exists(old_fpath):
                                os.remove(old_fpath)
                        existing_gdf = existing_gdf[~is_replaced]
                self.gdf = pd.concat(
                    [existing_gdf, new_gdf],
                    ignore_index=True,
                )

            parts_downloaded += 1
            granules_added += len(new_records)
            logger.info("Created %d new record(s).", len(new_records))

            self._write_index(self.gdf)

            if os.path.exists(self.db_fname):
                logger.info(
                    "Updated %s with %d total records.",
                    os.path.basename(self.db_fname),
                    len(self.gdf),
                )
            else:
                raise OSError(f"Failed to write {os.path.basename(self.db_fname)}")

        return DownloadSummary(
            parts_downloaded=parts_downloaded,
            parts_empty=parts_empty,
            parts_failed=parts_failed,
            granules_added=granules_added,
        )

    def bounds(self, axis: str, data_or_query: str = "data") -> tuple | None:
        """Return the min, max bounds of each entry in the database, on the axis requested ('x', 'y', or 't').

        Args:
            axis: The axis to get bounds for. Must be one of 'x', 'y', or 't'.
            data_or_query: Whether to use the data bounds or query-box bounds, by default "data".
                Must be one of "data" or "query".

        Raises:
            ValueError if parameters are invalid.

        Returns:
            list or None
                A 2-tuple containing (min, max) values for the requested axis.
                Returns None if no data is available or if invalid parameters are provided.

        """
        gdf = self.open_gdf()
        if gdf is None or len(gdf) == 0:
            return None

        data_or_query = data_or_query.lower().strip()
        if data_or_query == "data":
            base = "data_bbox"
        elif data_or_query == "query":
            base = "query_bbox"
        else:
            raise ValueError(
                "Invalid data_or_query parameter. Must be one of 'data' or 'query'.",
            )

        axis = axis.lower().strip()
        if axis == "x":
            mins = gdf[f"{base}_xmin"]
            maxs = gdf[f"{base}_xmax"]
        elif axis == "y":
            mins = gdf[f"{base}_ymin"]
            maxs = gdf[f"{base}_ymax"]
        elif axis == "t":
            mins = gdf[f"{base}_tmin"].astype(int)
            maxs = gdf[f"{base}_tmax"].astype(int)
        else:
            raise ValueError("Invalid axis parameter. Must be one of 'x', 'y', or 't'.")

        return mins, maxs

    def unique_bboxes(
        self,
        gdf: pd.DataFrame | None = None,
        data_or_query: str = "query",
    ) -> list | None:
        """Return a numpy array of unique query bounding boxes in the database.

        This is useful to see what query bounding-boxes have already been populated in the database.

        Args:
            gdf: An already-loaded granule index to read the boxes from. When None
                (the default), the database index is opened with open_gdf().
            data_or_query: Whether to use the data bounds or query-box bounds, by default "query".
                Must be one of "data" or "query".

        Raises:
            ValueError: If data_or_query parameter is invalid or not one of 'data' or 'query'.

        Returns:
            list or None
                List of unique bounding boxes from the database, where each box is a 6-tuple
                containing (xmin, xmax, ymin, ymax, tmin, tmax).
                Returns None if no data is available in the database.

        """
        if gdf is None:
            gdf = self.open_gdf()
        if gdf is None or len(gdf) == 0:
            return None

        data_or_query = data_or_query.lower().strip()
        if data_or_query == "data":
            base = "data_bbox"
        elif data_or_query == "query":
            base = "query_bbox"
        else:
            raise ValueError(
                "Invalid data_or_query parameter. Must be one of 'data' or 'query'.",
            )

        # Build a (xmin, xmax, ymin, ymax, tmin, tmax) tuple per row from the
        # scalar bbox columns, keep the unique ones, and cast the dates to int.
        cols = list(self._bbox_cols(base))
        # Return it as a list of bbox tuples.
        return sorted(
            {
                (
                    float(r[0]),
                    float(r[1]),
                    float(r[2]),
                    float(r[3]),
                    int(r[4]),
                    int(r[5]),
                )
                for r in gdf[cols].itertuples(index=False)
            },
        )

    def delete_cache(
        self,
        delete_everything: bool = False,
        delete_cmr: bool = True,
        delete_already_processed_txt: bool = True,
        delete_cudem_cache: bool = True,
        cache_subdir: str | None = None,
    ) -> None:
        """Delete the icesat-2 data downloads and clears the cache directory."""
        # If we only want to get rid of previous ICESat-2 downloads, clearing the CMR sub-directory will do that.
        if cache_subdir is None:
            cache_dir = self.icesat2_download_dir
        else:
            cache_dir = os.path.join(self.icesat2_download_dir, cache_subdir)

        if delete_everything and os.path.exists(cache_dir):
            for fname in [os.path.join(cache_dir, fn) for fn in os.listdir(cache_dir)]:
                shutil.rmtree(fname)

        else:
            if delete_cmr:
                if os.path.exists(os.path.join(cache_dir, "cmr")):
                    shutil.rmtree(os.path.join(cache_dir, "cmr"))
                if os.path.exists(os.path.join(cache_dir, ".cudem_cache", "cmr")):
                    shutil.rmtree(os.path.join(cache_dir, ".cudem_cache", "cmr"))

            if delete_already_processed_txt and os.path.exists(
                os.path.join(cache_dir, "already_processed.txt"),
            ):
                os.remove(os.path.join(cache_dir, "already_processed.txt"))

            if delete_cudem_cache and os.path.exists(
                os.path.join(cache_dir, ".cudem_cache"),
            ):
                shutil.rmtree(os.path.join(cache_dir, ".cudem_cache"))

    @staticmethod
    def bbox_valid(bbox: list | tuple) -> bool:
        """Validate a bounding box. Make sure all min-max values are correctly ordered.

        Args:
            bbox: A 6-item bounding box in [xmin, xmax, ymin, ymax, tmin, tmax] format where t is YYYYMMDD.
                In each case, the following cases must be true:
                    xmin < mmax
                    ymin < ymax
                    tmin <= tmax

        Returns:
            Boolean, True if all conditions are met, False if not.

        """
        xmin, xmax, ymin, ymax, tmin, tmax = bbox
        return (xmin < xmax) and (ymin < ymax) and (tmin <= tmax)

    def filter_query_bbox(self, query_bbox: list | tuple) -> list[tuple]:
        """Given an (x,y,t) ICESat-2 bounding box, remove existing regions and return bboxes for the rest of the data.

        Args:
            query_bbox: The input bounding box to filter, in [xmin, xmax, ymin, ymax, tmin, tmax] format where t is YYYYMMDD.

        Raises:
            ValueError: If query_bbox is not in the correct format or contains invalid values.

        Returns:
            List of bounding boxes that represent areas not already in the database,
                where each box is a 6-tuple containing (xmin, xmax, ymin, ymax, tmin, tmax).
                tmin and tmax are in YYYYMMDD format and are inclusive.
                Returns an empty list if the entire query_bbox is already present in the database.

        """
        return self.filter_query_bboxes([query_bbox])

    def filter_query_bboxes(self, query_bboxes: list | tuple) -> list[tuple]:
        """Remove the regions already in the database from several (x,y,t) bounding boxes at once.

        The same as :meth:`filter_query_bbox`, but the database's existing coverage
        is read once and subtracted from every box, and the remainders of all the
        boxes are merged together before being returned.

        Args:
            query_bboxes: The bounding boxes to filter, each in
                [xmin, xmax, ymin, ymax, tmin, tmax] format where t is YYYYMMDD.

        Raises:
            ValueError: If any box is not in the correct format or contains invalid values.

        Returns:
            List of (xmin, xmax, ymin, ymax, tmin, tmax) boxes covering the parts of
                the input boxes not already in the database, or an empty list if all
                of them are.

        """
        for query_bbox in query_bboxes:
            if not self.bbox_valid(query_bbox):
                raise ValueError(
                    "query_bbox must be a non-zero-volume valid 6-tuple or 6-value bbox, with values in the correct order.",
                )

        # First, get a list of the active unique query cuboids within the current database
        existing_bboxes = self.unique_bboxes(data_or_query="query")
        if existing_bboxes is None or len(existing_bboxes) == 0:
            return list(query_bboxes)

        # For the purpose of merging, increase the tmaxes by 1 day to make all boxes non-inclusive
        # (This makes adjoining bounding-boxes actually border each other in coordinate space rather than be 1 day apart)
        # e_bboxes = [tuple(bb[:5]) + (self.increment_yyyymmdd_by_n(bb[5], 1),) for bb in existing_bboxes]

        # Simplify by merging these bboxes together (could have been gathered on a number of queries).
        e_bboxes = ivert.utils.cuboid_funcs.merge_cuboids(
            existing_bboxes,
            bbox_order="axis",
        )

        # Now, increment the query_box tmax by 1 to make it non-inclusive as well (for cuboid subtraction)
        # query_bbox = tuple(query_bbox[:5]) + (self.increment_yyyymmdd_by_n(query_bbox[5], 1),)

        # Now do a cuboid subtraction of the query bboxes by all the e_bboxes:
        query_bboxes = list(query_bboxes)
        for e_bbox in e_bboxes:
            new_bboxes = []
            for q_bbox in query_bboxes:
                new_bboxes.extend(
                    ivert.utils.cuboid_funcs.subtract_cuboids(
                        q_bbox,
                        e_bbox,
                        bbox_order="axis",
                    ),
                )

            query_bboxes = new_bboxes

        # Now, decrement the tmax day by 1 to make the ranges inclusive again.
        # query_bboxes = [tuple(bb[:5]) + (self.increment_yyyymmdd_by_n(bb[5], -1),) for bb in query_bboxes]

        # Do a quick merger on all the remaining bboxes to make sure they're simplified
        return ivert.utils.cuboid_funcs.merge_cuboids(
            query_bboxes,
            bbox_order="axis",
        )

    @staticmethod
    def increment_yyyymmdd_by_n(yyyymmdd: float | str, days: int) -> int:
        """Increment a YYYYMMDD integer by N calendar days (positive or negative).."""
        ymd = int(yyyymmdd)
        ymd_dt = datetime.datetime.strptime(str(ymd), "%Y%m%d").replace(
            tzinfo=datetime.UTC,
        ) + datetime.timedelta(days=int(days))
        return int(ymd_dt.strftime("%Y%m%d"))


def _tile_edges(
    vmin: float,
    vmax: float,
    tile_size: float,
    sliver_fraction: float,
) -> list[float]:
    """Return the cell edges that split [vmin, vmax] into tiles of ``tile_size``.

    The tiles are anchored at ``vmin`` and the last one is clipped to ``vmax``. If
    that clipped tile is narrower than ``sliver_fraction`` of a full tile, it is
    merged into its neighbour, which then grows to a little over ``tile_size``.
    """
    edges = [float(v) for v in np.arange(vmin, vmax, tile_size)]
    edges.append(float(vmax))
    # Floating-point stepping can land an edge a hair short of vmax; that is
    # also a sliver and is merged away by the same rule.
    if len(edges) >= 3 and (edges[-1] - edges[-2]) < sliver_fraction * tile_size:
        del edges[-2]
    return edges


def tile_geometry_into_bboxes(
    geometry: shapely.Geometry,
    tile_size_deg: float = 2.0,
    sliver_fraction: float = 0.25,
) -> list[tuple[float, float, float, float]]:
    """Cover a WGS84 geometry with as few axis-aligned rectangles as practical.

    The geometry's bounding box is cut into ``tile_size_deg`` squares, anchored at
    its south-west corner and clipped to the box on the north and east. A clipped
    edge row or column narrower than ``sliver_fraction`` of a full tile is merged
    into its neighbour, so no request is a thin sliver. Squares whose interior does
    not overlap the geometry are dropped, and the rest are merged into a minimal
    set of rectangles with :func:`ivert.utils.cuboid_funcs.merge_cuboids`, asking
    it to prefer north-south strips when either orientation would do. ICESat-2
    ground tracks run closer to north-south than east-west, so tall rectangles cut
    across fewer passes, and each pass then needs fewer granule subsets.

    Args:
        geometry: A shapely geometry in WGS84 (EPSG:4326). Reproject first; the
            tiles are cut in degrees.
        tile_size_deg: The side of the squares the bounding box is cut into.
        sliver_fraction: Clipped edge tiles narrower than this fraction of a full
            tile are merged into their neighbour.

    Returns:
        (xmin, xmax, ymin, ymax) rectangles in WGS84, together covering the
        geometry, with no two overlapping. Empty if the geometry is empty.
    """
    if geometry is None or geometry.is_empty:
        return []

    xmin, ymin, xmax, ymax = (float(v) for v in geometry.bounds)
    xedges = _tile_edges(xmin, xmax, tile_size_deg, sliver_fraction)
    yedges = _tile_edges(ymin, ymax, tile_size_deg, sliver_fraction)

    # Squares that share only an edge or a corner with the geometry hold none
    # of it, so they are not kept: "touches" is the interiors-disjoint case.
    prepared = shapely.prepared.prep(geometry)
    squares = []
    for y0, y1 in itertools.pairwise(yedges):
        for x0, x1 in itertools.pairwise(xedges):
            square = shapely.box(x0, y0, x1, y1)
            if prepared.intersects(square) and not prepared.touches(square):
                squares.append((x0, x1, y0, y1))

    if not squares:
        return []

    # merge_cuboids works in three dimensions; give the squares a unit thickness.
    cuboids = [(x0, x1, y0, y1, 0.0, 1.0) for (x0, x1, y0, y1) in squares]
    merged = ivert.utils.cuboid_funcs.merge_cuboids(
        cuboids,
        bbox_order="axis",
        prefer="column",
    )
    return [(x0, x1, y0, y1) for (x0, x1, y0, y1, _z0, _z1) in merged]


def split_bbox_into_parts(
    bbox: list | tuple,
    tile_size_deg: float = 2.0,
    max_tile_scale_factor: float = 1.5,
) -> list | None:
    """Split a bounding box into parts of size approximately deg_size degrees.."""
    # if we included a 6-value bbox, save the last two and append them at the end.
    tmin, tmax = None, None
    if len(bbox) == 6:
        tmin, tmax = bbox[4], bbox[5]
        bbox = bbox[:4]
    assert len(bbox) == 4, "bbox must be a 4-tuple or 6-tuple."

    xmin, xmax, ymin, ymax = bbox
    max_deg_size = tile_size_deg * max_tile_scale_factor

    xbins = np.arange(xmin, xmax, tile_size_deg)
    ybins = np.arange(ymin, ymax, tile_size_deg)

    if xbins[-1] < xmax:
        if len(xbins) == 1 or ((xmax - xbins[-2]) > max_deg_size):
            xbins = np.append(xbins, xmax)
        else:
            xbins[-1] = xmax

    if ybins[-1] < ymax:
        if len(ybins) == 1 or ((ymax - ybins[-2]) > max_deg_size):
            ybins = np.append(ybins, ymax)
        else:
            ybins[-1] = ymax

    binxs, binys = np.meshgrid(xbins, ybins)
    bin_xmins = binxs[:-1, :-1].flatten()
    bin_xmaxs = binxs[1:, 1:].flatten()
    bin_ymins = binys[:-1, :-1].flatten()
    bin_ymaxs = binys[1:, 1:].flatten()

    if tmin is not None and tmax is not None:
        bboxes = [
            (
                float(xbmin),
                float(xbmax),
                float(ybmin),
                float(ybmax),
                int(tmin),
                int(tmax),
            )
            for (xbmin, xbmax, ybmin, ybmax) in zip(
                bin_xmins,
                bin_xmaxs,
                bin_ymins,
                bin_ymaxs,
                strict=True,
            )
        ]
    else:
        bboxes = [
            (float(xbmin), float(xbmax), float(ybmin), float(ybmax))
            for (xbmin, xbmax, ybmin, ybmax) in zip(
                bin_xmins,
                bin_xmaxs,
                bin_ymins,
                bin_ymaxs,
                strict=True,
            )
        ]

    return bboxes


def _cmd_list():
    """Implementation of the 'list' subcommand."""
    import tabulate as tabulate_mod

    db = IS2Database()
    gdf = db.open_gdf()

    if gdf is None or len(gdf) == 0:
        logger.info("No granules in database.")
        return

    rows = []
    for _, row in gdf.iterrows():
        rows.append(
            [
                row["filename"],
                row["numphotons"],
                row["numphotons_ground"],
                row["numphotons_bathy_floor"],
                row["numphotons_bathy_surface"],
            ],
        )

    headers = ["File", "Total", "Ground", "BathyFloor", "BathySurf"]
    # Formatted table output for a person reading the terminal, not a log record:
    # keep it on stdout so it can be piped, and unadorned by any level prefix.
    print(  # noqa: T201
        tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple", intfmt=","),
    )
    logger.info("\n%s granule(s)  —  db: %s", len(gdf), db.db_fname)


def _cmd_delete(delete_all):
    """Implementation of the 'delete' subcommand."""
    db = IS2Database()

    if os.path.exists(db.db_fname):
        os.remove(db.db_fname)
        logger.info("Deleted %s", db.db_fname)
    else:
        logger.info("Not found (skipping): %s", db.db_fname)

    if delete_all:
        nc_files = (
            [
                os.path.join(db.granules_dir, fn)
                for fn in os.listdir(db.granules_dir)
                if os.path.splitext(fn)[-1].lower() == ".nc"
            ]
            if os.path.isdir(db.granules_dir)
            else []
        )
        if nc_files:
            for fpath in sorted(nc_files):
                os.remove(fpath)
            logger.info(
                "Deleted %s .nc granule file(s) from %s",
                len(nc_files),
                db.granules_dir,
            )
        else:
            logger.info("No .nc files found in %s", db.granules_dir)


def _cmd_rebuild():
    """Implementation of the 'rebuild' subcommand."""
    db = IS2Database()
    gdf = db.create_new_database(populate=True, overwrite=True)
    n = len(gdf)
    logger.info(
        "Rebuilt ivert database index with %d granule%s.",
        n,
        "" if n == 1 else "s",
    )


if __name__ == "__main__":
    import click

    @click.group(
        name="icesat2_database_v2",
        help="Manage the local ICESat-2 photon granule database.",
    )
    def _cli():
        pass

    @_cli.command("list", help="List granules currently in the database.")
    def _list_command():
        _cmd_list()

    @_cli.command("delete", help="Delete the NetCDF database index file.")
    @click.option(
        "--all",
        "delete_all",
        is_flag=True,
        help="Also delete all .nc granule data files.",
    )
    def _delete_command(delete_all):
        _cmd_delete(delete_all)

    @_cli.command(
        "rebuild",
        help="Rebuild the database from existing .nc granule files."
        "Useful if the .nc files have been modified at all, and/or if you suspect"
        " the overview information has become inaccurate.",
    )
    def _rebuild_command():
        _cmd_rebuild()

    _cli()
