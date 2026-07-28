# Functionality for reading ICESat-2 data and saving it in a tiled database.

import datetime
import logging
import os
import re
import shutil

import dateparser
import fetchez
import fetchez.core
import fetchez.spatial
import globato
import numpy
import pandas
import xarray
from fetchez.modules.earthdata import IceSat2 as _FetchezIceSat2

import ivert.utils.configfile
import ivert.utils.cuboid_funcs
from ivert.icesat2_requests import ICESat2RequestsCSV

logger = logging.getLogger(__name__)

# ICESat-2 epoch: all delta_time values are seconds since 2018-01-01T00:00:00Z
_ICESAT2_EPOCH = datetime.datetime(2018, 1, 1, 0, 0, 0)


def _yyyymmdd_to_delta_time(yyyymmdd: int | str) -> float:
    """Convert a YYYYMMDD integer to ICESat-2 delta_time (seconds since 2018-01-01)."""
    return (
        datetime.datetime.strptime(str(int(yyyymmdd)), "%Y%m%d") - _ICESAT2_EPOCH
    ).total_seconds()


def _delta_time_to_yyyymmdd(delta_time: float) -> int:
    """Convert ICESat-2 delta_time (seconds since 2018-01-01) to a YYYYMMDD integer."""
    return int(
        (_ICESAT2_EPOCH + datetime.timedelta(seconds=float(delta_time))).strftime(
            "%Y%m%d",
        ),
    )


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
        "downloaded_on",
    )
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

    def __init__(self, ivert_config: ivert.utils.configfile.Config | None = None):
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

    def create_new_database(
        self,
        populate: bool = True,
        overwrite: bool = False,
    ) -> pandas.DataFrame:
        """Create a new database from scratch.

        Parameters
        ----------
        populate : bool
            Whether to populate the database with the data from the tiles.
        overwrite : bool
            Whether to overwrite the database if it already exists.

        Raises
        ------
        OSError if database file cannot be created or already exists and overwrite is False.

        Returns
        -------
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
            nc_files = sorted(
                [
                    os.path.join(self.granules_dir, fn)
                    for fn in os.listdir(self.granules_dir)
                    if os.path.splitext(fn)[-1].lower() == ".nc"
                ],
            )

            records = []
            for nc_fn in nc_files:
                meta = self._read_nc_metadata(nc_fn)
                if meta is not None:
                    records.append(meta)

            if records:
                gdf = pandas.DataFrame(records)[list(self._empty_db_dict().keys())]
            else:
                gdf = pandas.DataFrame(self._empty_db_dict()).drop(
                    labels=0,
                    axis="rows",
                )

        else:
            gdf = pandas.DataFrame(self._empty_db_dict()).drop(labels=0, axis="rows")

        self._write_index(gdf)
        if os.path.exists(self.db_fname):
            logger.info(
                "Created %s with %d records.",
                os.path.basename(self.db_fname),
                len(gdf),
            )
        else:
            raise OSError("Failed to create", os.path.basename(self.db_fname))

        # This becomes the new database for this object.
        self.gdf = gdf

        return gdf

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
            record[col] = int(attrs.get(col, 0))
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
    def _nc_filename(h5_fn: str, query_bbox: tuple) -> str:
        """Build a unique .nc filename by appending the query bbox to the granule base name.

        Format: <granule_base>_<W|E><xmin>_<W|E><xmax>_<S|N><ymin>_<S|N><ymax>_<tmin>_<tmax>.nc

        This ensures that the same granule downloaded for different query regions or time
        spans produces distinct files rather than overwriting each other.
        """

        def _lon_tag(v):
            return f"{'W' if v < 0 else 'E'}{abs(float(v)):09.5f}"

        def _lat_tag(v):
            return f"{'S' if v < 0 else 'N'}{abs(float(v)):08.5f}"

        base = os.path.splitext(os.path.basename(h5_fn))[0]
        xmin, xmax, ymin, ymax, tmin, tmax = query_bbox
        suffix = (
            f"_{_lon_tag(xmin)}_{_lon_tag(xmax)}"
            f"_{_lat_tag(ymin)}_{_lat_tag(ymax)}"
            f"_{int(tmin)}_{int(tmax)}"
        )
        return base + suffix + ".nc"

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
    def _h5_along_track_m(h5_fn: str, beams) -> pandas.DataFrame:
        """Return per-photon cumulative along-track distance (m) for the given beams.

        Cumulative distance = sum of segment_length values up to each photon's segment
        (from geolocation/segment_length) plus the photon's offset within that segment
        (from heights/dist_ph_along).

        Returns a DataFrame with columns [laser, delta_time, x, y, along_track_m].
        """
        import h5py

        dfs = []
        with h5py.File(h5_fn, "r") as f:
            for beam in beams:
                try:
                    delta_time = f[f"{beam}/heights/delta_time"][...]
                    lon = f[f"{beam}/heights/lon_ph"][...]
                    lat = f[f"{beam}/heights/lat_ph"][...]
                    dist_ph_along = f[f"{beam}/heights/dist_ph_along"][...]
                    ph_index_beg = f[f"{beam}/geolocation/ph_index_beg"][...]
                    seg_length = f[f"{beam}/geolocation/segment_length"][...]
                except KeyError:
                    continue

                # Cumulative distance at the start of each segment
                seg_cumul_start = numpy.concatenate(
                    [[0.0], numpy.cumsum(seg_length[:-1])],
                )

                # Map each photon to its segment via ph_index_beg
                n = len(delta_time)
                seg_of_ph = numpy.clip(
                    numpy.searchsorted(ph_index_beg, numpy.arange(n), side="right") - 1,
                    0,
                    len(ph_index_beg) - 1,
                )

                dfs.append(
                    pandas.DataFrame(
                        {
                            "laser": beam,
                            "delta_time": delta_time,
                            "x": lon,
                            "y": lat,
                            "along_track_m": seg_cumul_start[seg_of_ph] + dist_ph_along,
                        },
                    ),
                )

        return (
            pandas.concat(dfs, ignore_index=True)
            if dfs
            else pandas.DataFrame(
                columns=["laser", "delta_time", "x", "y", "along_track_m"],
            )
        )

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
    _EPSG_TO_GLOBATO_VERTICAL_DATUM = {
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
        """Classify an ATL03 HDF5 file with globato and save the result as NetCDF.

        The output .nc file contains only the photon classes in classes_to_keep, plus
        rich metadata attributes so the database can be rebuilt from headers alone.

        Returns the metadata dict, or None if no photons survived filtering.
        """
        if os.path.exists(nc_fn) and not overwrite:
            return self._read_nc_metadata(nc_fn)

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

        chunks = []
        for chunk in stream:
            chunks.append(pandas.DataFrame(chunk))

        if not chunks:
            return None

        df = pandas.concat(chunks, ignore_index=True)
        df.rename(columns={"ph_h_classed": "class_code"}, inplace=True)

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
        ]
        df = df[[c for c in keep_cols if c in df.columns]].copy()

        # Compute metadata for file attributes and the database record.
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
        metadata_attrs = {
            "granule_id": os.path.splitext(os.path.basename(nc_fn))[0],
            "source_granule": os.path.splitext(os.path.basename(h5_fn))[0],
            "laser_name": "all",
            "query_bbox": list(query_bbox),
            "data_bbox": [xmin, xmax, ymin, ymax, tmin, tmax],
            "zbounds": [zmin, zmax],
            "numphotons": len(df),
            "numphotons_unclassified": int(numpy.count_nonzero(cc == -1)),
            "numphotons_noise": int(numpy.count_nonzero(cc == 0)),
            "numphotons_ground": int(numpy.count_nonzero(cc == 1)),
            "numphotons_canopy": int(numpy.count_nonzero(cc == 2)),
            "numphotons_canopy_top": int(numpy.count_nonzero(cc == 3)),
            "numphotons_ice_surface": int(numpy.count_nonzero(cc == 6)),
            "numphotons_buildings": int(numpy.count_nonzero(cc == 7)),
            "numphotons_bathy_floor": int(numpy.count_nonzero(cc == 40)),
            "numphotons_bathy_surface": int(numpy.count_nonzero(cc == 41)),
            "numphotons_inland_water_surface": int(numpy.count_nonzero(cc == 42)),
            "downloaded_on": int(datetime.datetime.now().strftime("%Y%m%d")),
            "horizontal_datum": "EPSG:4326",
            "vertical_datum": vertical_datum,
        }

        # Add per-photon cumulative along-track distance from h5 geolocation data.
        # Merge on (laser, delta_time, x, y) — the four fields that uniquely identify
        # a photon across beams, since all beams share delta_time values.
        if "laser" in df.columns:
            dist_df = self._h5_along_track_m(h5_fn, df["laser"].unique().tolist())
            if not dist_df.empty:
                df = df.merge(dist_df, on=["laser", "delta_time", "x", "y"], how="left")

        # Build xarray Dataset and embed metadata as global attributes.
        xr_ds = xarray.Dataset.from_dataframe(df)
        xr_ds.attrs = metadata_attrs

        os.makedirs(
            os.path.dirname(nc_fn) if os.path.dirname(nc_fn) else ".",
            exist_ok=True,
        )
        xr_ds.to_netcdf(nc_fn)
        progress = (
            f"{granule_num}/{total_granules} "
            if granule_num is not None and total_granules is not None
            else ""
        )
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
                numpy.array(
                    ["" if v is None else str(v) for v in df[col]],
                    dtype=object,
                )
                if n
                else numpy.array([], dtype=object)
            )
            data_vars[col] = ("record", values)

        int_cols = (*self._INDEX_INT_COLS, *self._INDEX_BBOX_INT_COLS)
        for col in int_cols:
            values = (
                numpy.asarray(df[col].to_list(), dtype="int64")
                if n
                else numpy.array([], dtype="int64")
            )
            data_vars[col] = ("record", values)

        float_cols = (*self._INDEX_BBOX_FLOAT_COLS, *self._INDEX_ZBOUNDS_COLS)
        for col in float_cols:
            values = (
                numpy.asarray(df[col].to_list(), dtype="float64")
                if n
                else numpy.array([], dtype="float64")
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
            os.path.dirname(self.db_fname) if os.path.dirname(self.db_fname) else ".",
            exist_ok=True,
        )
        ds.to_netcdf(self.db_fname, encoding=encoding)

    @classmethod
    def read_index_file(cls, index_fname: str) -> pandas.DataFrame:
        """Read any IVERT NetCDF index file into a plain pandas DataFrame.

        Every column comes back as a numpy array with no per-row Python loops and
        no geometry reconstruction, which is what makes this read fast; spatial
        filtering is done downstream with vectorized bbox comparisons on the
        data_bbox_* / query_bbox_* columns. Columns are in the canonical order
        given by _empty_db_dict().
        """
        with xarray.open_dataset(index_fname) as ds:
            ds = ds.load()

        data = {}
        for col in cls._INDEX_STR_COLS:
            data[col] = ds[col].values.astype(str)
        for col in (
            *cls._INDEX_INT_COLS,
            *cls._INDEX_BBOX_INT_COLS,
            *cls._INDEX_BBOX_FLOAT_COLS,
            *cls._INDEX_ZBOUNDS_COLS,
        ):
            data[col] = ds[col].values

        df = pandas.DataFrame(data)
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
        verbose: bool = True,
    ):
        """Get the index DataFrame from the database.

        Parameters
        ----------
        force_reread : bool
            If True, read the file again even if we've already read the database into memory.
        verbose: bool
            Output text if the file was read.

        Returns
        -------
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
        if verbose:
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
    ) -> pandas.DataFrame:
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
    ) -> pandas.DataFrame:
        """Read classified photons from a NetCDF granule file.

        Parameters
        ----------
        granule_fn : str
            Path to a processed .nc granule file in the granules directory.
        subset_bbox : list or tuple, optional
            6-value bounding box (xmin, xmax, ymin, ymax, tmin, tmax) where t is YYYYMMDD.
        photon_classes : list or tuple, optional
            Photon class codes to return. Defaults to (1, 40) (ground and bathy floor).

        Returns
        -------
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
            return True
        except TypeError:
            return False

    def query_photons(
        self,
        bbox: list | tuple | None = None,
        photon_classes: list | tuple | None = (1, 6, 40),
        min_bathy_confidence=0.75,
        min_confidence_level: int = 1,
        omit_bboxes=[],
        # download_new_data: bool = False,
    ) -> pandas.DataFrame | None:
        """Query the database for photons in a given bounding box and date range.

        Parameters
        ----------
            bbox : list, tuple, or None
                Bounding box to limit the data to, in [xmin, xmax, ymin, ymax, tmin, tmax]. Must be in WGS84 (EPSG: 4326)
                coordinates, and yyyymmdd integers for the date. Date range is not inclusive of the max date.
            photon_classes : list, tuple, or None
                Photon classes to include in the query. See globato/streams/readers/icesat2.py for the full list.
                Defaults to (1, 6, 40) (ground, land_ice, and bathy_floor photons).
            min_bathy_confidence : float
                The minimum ATL24 confidence for bathymetric (class 40) photons to include (0.0–1.0).
            min_confidence_level : int
                The minimum ATL03 signal confidence level to include (1–4). 1 keeps all photons.
            # download_new_data : bool
            #     Whether to download new ICESat-2 data from NASA if the current database doesn't contain the entire bounding box.

        Returns
        -------
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
            numpy.count_nonzero(fnames.apply(os.path.exists)),
            f"{gdf_subset['numphotons_ground'].sum():,}",
            f"{gdf_subset['numphotons_bathy_floor'].sum():,}",
        )

        granule_dfs = []
        for idx, granule_line in gdf_subset.iterrows():
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

        photons_df = pandas.concat(granule_dfs, ignore_index=True)

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
        if len(omit_bboxes) in (4, 6) and not numpy.any(
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
                f"{numpy.count_nonzero(photons_df['class_code'] == 1):,}",
                f"{numpy.count_nonzero(photons_df['class_code'] == 40):,}",
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
        elif isinstance(date, datetime.datetime) or isinstance(date, datetime.date):
            return int(date.strftime("%Y%m%d"))
        else:
            raise ValueError(
                "Date must be an int, str, datetime.datetime, or datetime.date.",
            )

    def query_granules(self, bbox: list | tuple) -> pandas.DataFrame | None:
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
        gdf = self.open_gdf(verbose=False)
        if gdf is not None and len(gdf) > 0:
            if "horizontal_datum" in gdf.columns and "vertical_datum" in gdf.columns:
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
        split_big_bboxes: bool = True,
        tile_size_deg=2.0,
        max_tile_scale_factor=1.5,
        min_bathy_confidence=0.01,
        min_confidence_level: int = 1,
        cache_subdir: str | None = None,
        replace: bool = False,
    ):
        """Download ICESat-2 ATL03 granules from NASA using fetchez and register them in the database.

        Downloads raw HDF5 files into granules_dir; classification is deferred to read time via globato.
        Only downloads granules covering bboxes not already in the database, unless 'replace' is True,
        in which case the full requested bbox is (re-)downloaded and any existing granules it overlaps
        are replaced with the newly-downloaded data.
        """
        # Validate the configured water surface and derive the target vertical datum.
        vertical_datum_cfg = self._validate_vertical_datum(
            self.config.icesat2_vertical_datum,
        )
        target_vd = self._vertical_datum_to_vertical_epsg(vertical_datum_cfg)

        # Reject the download if existing records use a different datum.
        existing_gdf_check = self.open_gdf(verbose=False)
        if existing_gdf_check is not None and len(existing_gdf_check) > 0:
            if "vertical_datum" in existing_gdf_check.columns:
                existing_vd_vals = existing_gdf_check["vertical_datum"].dropna()
                existing_vd_vals = existing_vd_vals[existing_vd_vals != ""]
                if len(existing_vd_vals) > 0:
                    existing_vd = str(existing_vd_vals.iloc[0])
                    existing_hd = "EPSG:4326"
                    if "horizontal_datum" in existing_gdf_check.columns:
                        existing_hd_vals = existing_gdf_check[
                            "horizontal_datum"
                        ].dropna()
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
                        return

        if replace:
            bboxes = [tuple(bbox)]
            logger.info(
                "--replace enabled: re-downloading the full requested region and "
                "overwriting any overlapping existing granules.",
            )
        else:
            bboxes = self.filter_query_bbox(bbox)

            if len(bboxes) == 0:
                logger.info(
                    "All required granules already exist in the database. Nothing new to download.",
                )
                return

            if not (len(bboxes) == 1 and tuple(bboxes[0]) == tuple(bbox)):
                logger.info(
                    "Existing database coverage partially overlaps the requested region. "
                    "Downloading only the missing sub-region(s) (%d area(s) to fill).",
                    len(bboxes),
                )

        if split_big_bboxes:
            bboxes_split = []
            for bb in bboxes:
                bboxes_split.extend(
                    split_bbox_into_parts(
                        bb,
                        tile_size_deg=tile_size_deg,
                        max_tile_scale_factor=max_tile_scale_factor,
                    ),
                )
            bboxes = bboxes_split

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
            time_start = datetime.datetime.strptime(
                str(int(sbbox[4])),
                "%Y%m%d",
            ).strftime("%Y-%m-%dT00:00:00")
            time_end = datetime.datetime.strptime(
                str(int(sbbox[5])),
                "%Y%m%d",
            ).strftime("%Y-%m-%dT00:00:00")

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
                _ERROR_STATES = {"complete_with_errors", "failed", "canceled"}
                cached_job_id = cached["jobID"]
                current_status = mod.harmony_ping_for_status(cached_job_id)
                current_state = (
                    current_status.get("status", "") if current_status else ""
                )
                if current_state in _ERROR_STATES:
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
                continue

            logger.info(
                "Downloaded %d ATL03 granule(s). Classifying and saving as NetCDF...",
                len(h5_files),
            )

            use_external_masks = True

            existing_gdf = self.open_gdf(verbose=False)
            existing_filenames = (
                set(existing_gdf["filename"].values)
                if existing_gdf is not None
                else set()
            )

            new_records = []
            files_to_process = []
            for h5_src in h5_files:
                nc_basename = self._nc_filename(h5_src, sbbox)
                nc_dest = os.path.join(self.granules_dir, nc_basename)
                if nc_basename in existing_filenames and not replace:
                    logger.info("Skipping %s (already in database).", nc_basename)
                else:
                    files_to_process.append((h5_src, nc_dest))

            for granule_num, (h5_src, nc_dest) in enumerate(files_to_process, start=1):
                meta = self._process_h5_to_nc(
                    h5_src,
                    nc_dest,
                    query_bbox=sbbox,
                    classes_to_keep=classes_to_keep,
                    min_confidence_level=min_confidence_level,
                    granule_num=granule_num,
                    total_granules=len(files_to_process),
                    use_external_masks=use_external_masks,
                )
                if meta is not None:
                    new_records.append(meta)
                else:
                    logger.info(
                        "%d/%d No valid classified photons in %s.",
                        granule_num,
                        len(files_to_process),
                        os.path.basename(nc_dest),
                    )

            if not new_records:
                continue

            new_gdf = pandas.DataFrame(new_records)[list(self._empty_db_dict().keys())]
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
                    bbox_overlaps = pandas.Series(
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
                self.gdf = pandas.concat(
                    [existing_gdf, new_gdf],
                    ignore_index=True,
                )

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

    def bounds(self, axis: str, data_or_query: str = "data") -> tuple | None:
        """Return the min, max bounds of each entry in the database, on the axis requested ('x', 'y', or 't').

        Parameters
        ----------
        axis : str
            The axis to get bounds for. Must be one of 'x', 'y', or 't'.
        data_or_query : str, optional
            Whether to use the data bounds or query-box bounds, by default "data".
            Must be one of "data" or "query".

        Raises
        ------
        ValueError if parameters are invalid.

        Returns
        -------
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
        gdf: pandas.DataFrame | None = None,
        data_or_query: str = "query",
    ) -> list | None:
        """Return a numpy array of unique query bounding boxes in the database.

        This is useful to see what query bounding-boxes have already been populated in the database.

        Parameters
        ----------
        data_or_query : str, optional
            Whether to use the data bounds or query-box bounds, by default "query".
            Must be one of "data" or "query".

        Raises
        ------
        ValueError
            If data_or_query parameter is invalid or not one of 'data' or 'query'.

        Returns
        -------
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
        bboxes = sorted(
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

        # Return it as a list of bbox tuples.
        return bboxes

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

        Parameters
        ----------
        bbox : list or tuple
            A 6-item bounding box in [xmin, xmax, ymin, ymax, tmin, tmax] format where t is YYYYMMDD.
            In each case, the following cases must be true:
                xmin < mmax
                ymin < ymax
                tmin <= tmax

        Returns
        -------
            Boolean, True if all conditions are met, False if not.

        """
        xmin, xmax, ymin, ymax, tmin, tmax = bbox
        return (xmin < xmax) and (ymin < ymax) and (tmin <= tmax)

    def filter_query_bbox(self, query_bbox: list | tuple) -> list[tuple]:
        """Given an (x,y,t) ICESat-2 bounding box, remove existing regions and return bboxes for the rest of the data.

        Parameters
        ----------
        query_bbox : list or tuple
            The input bounding box to filter, in [xmin, xmax, ymin, ymax, tmin, tmax] format where t is YYYYMMDD.

        Raises
        ------
        ValueError
            If query_bbox is not in the correct format or contains invalid values.

        Returns
        -------
        List of bounding boxes that represent areas not already in the database,
            where each box is a 6-tuple containing (xmin, xmax, ymin, ymax, tmin, tmax).
            tmin and tmax are in YYYYMMDD format and are inclusive.
            Returns an empty list if the entire query_bbox is already present in the database.

        """
        if not self.bbox_valid(query_bbox):
            raise ValueError(
                "query_bbox must be a non-zero-volume valid 6-tuple or 6-value bbox, with values in the correct order.",
            )

        # First, get a list of the active unique query cuboids within the current database
        existing_bboxes = self.unique_bboxes(data_or_query="query")
        if existing_bboxes is None or len(existing_bboxes) == 0:
            return [query_bbox]

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

        # Now do a cuboid subtraction of query_bbox by all the e_bboxes:
        query_bboxes = [query_bbox]
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

        # Do a quick merger on all the remaining bboxes to make sure they're simplified
        query_bboxes = ivert.utils.cuboid_funcs.merge_cuboids(
            query_bboxes,
            bbox_order="axis",
        )

        # Now, decrement the tmax day by 1 to make the ranges inclusive again.
        # query_bboxes = [tuple(bb[:5]) + (self.increment_yyyymmdd_by_n(bb[5], -1),) for bb in query_bboxes]

        return query_bboxes

    @staticmethod
    def increment_yyyymmdd_by_n(yyyymmdd: float | str, days: int) -> int:
        """Increment a YYYYMMDD integer by N calendar days (positive or negative).."""
        ymd = int(yyyymmdd)
        ymd_dt = datetime.datetime.strptime(str(ymd), "%Y%m%d") + datetime.timedelta(
            days=int(days),
        )
        return int(ymd_dt.strftime("%Y%m%d"))


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

    xbins = numpy.arange(xmin, xmax, tile_size_deg)
    ybins = numpy.arange(ymin, ymax, tile_size_deg)

    if xbins[-1] < xmax:
        if len(xbins) == 1 or ((xmax - xbins[-2]) > max_deg_size):
            xbins = numpy.append(xbins, xmax)
        else:
            xbins[-1] = xmax

    if ybins[-1] < ymax:
        if len(ybins) == 1 or ((ymax - ybins[-2]) > max_deg_size):
            ybins = numpy.append(ybins, ymax)
        else:
            ybins[-1] = ymax

    binxs, binys = numpy.meshgrid(xbins, ybins)
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
            )
        ]

    return bboxes


def _cmd_list():
    """Implementation of the 'list' subcommand."""
    import tabulate as tabulate_mod

    db = IS2Database()
    gdf = db.open_gdf(verbose=False)

    if gdf is None or len(gdf) == 0:
        print("No granules in database.")
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
    print(tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple", intfmt=","))
    print(f"\n{len(gdf)} granule(s)  —  db: {db.db_fname}")


def _cmd_delete(delete_all):
    """Implementation of the 'delete' subcommand."""
    db = IS2Database()

    if os.path.exists(db.db_fname):
        os.remove(db.db_fname)
        print(f"Deleted {db.db_fname}")
    else:
        print(f"Not found (skipping): {db.db_fname}")

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
            print(f"Deleted {len(nc_files)} .nc granule file(s) from {db.granules_dir}")
        else:
            print(f"No .nc files found in {db.granules_dir}")


def _cmd_rebuild():
    """Implementation of the 'rebuild' subcommand."""
    db = IS2Database()
    gdf = db.create_new_database(populate=True, overwrite=True)
    print(f"Rebuilt database with {len(gdf)} granule(s).")


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
