"""Filter misclassified ATL24 bathymetry-floor photons (class 40) at validation time.

ATL24 labels some photons as seafloor that are not. Two kinds are common: photons a
metre or two below the sea surface over water hundreds of metres deep, and photons
tens of metres below sea level under dry land. None of ATL24's own per-photon fields
separate them from real seafloor, but independent context does. Each rule below
compares a class-40 photon against a reference bathymetry (ETOPO 2022 by default)
or the OpenStreetMap coastline landmask, and drops the photon if it fails:

    deep       The shallowest reference cell within 'ref_window_m' is deeper than
               'max_depth_m', well past the depth ICESat-2 can see the seafloor.
    offshore   Outside the landmask, within 'near_surface_m' of the sea surface, and
               more than 'min_coast_dist_m' from the coastline: surface returns in
               open water.
    reference  Shallower than the reference bathymetry by more than 'ref_tolerance_m'.
    land       Inside the landmask and more than 'land_max_below_sl_m' below sea level.

Only class-40 photons are touched. Every threshold is a setting in the IVERT config
and a flag on 'ivert validate'.
"""

import dataclasses
import json
import logging
import math
import os
from typing import Self

import numpy as np
import pandas as pd
import pyproj
import rasterio
import rasterio.enums
import rasterio.merge
import rasterio.transform
import rasterio.vrt
import shapely
from scipy import ndimage
from scipy.spatial import cKDTree

import ivert.landmask
import ivert.transform_points
import ivert.utils.logging_config

logger = logging.getLogger(__name__)

BATHY_FLOOR_CLASS = 40

# Rule names, in the order they are reported.
RULES = ("deep", "offshore", "reference", "land")

ETOPO_DESCRIPTION = 'ETOPO 2022 15" surface elevation (EGM2008)'

# The attribute that carries a BathyFilterReport on the results .h5 file.
H5_REPORT_ATTR = "bathy_filter_report"

_M_PER_DEG = 111_320.0

# Spacing of the points along the coastline used to measure distance to it. The
# distance is off by at most half of this.
_COAST_POINT_SPACING_M = 25.0

# The widest window, in raster cells, that 'deep' runs a maximum filter over. A
# finer reference raster is first reduced by block maximum until the window fits.
_MAX_WINDOW_CELLS = 51


@dataclasses.dataclass(frozen=True)
class BathyFilterSettings:
    """Which bathymetry filters to run, and their thresholds.

    Build one from the IVERT config with from_config(), which fills in any value not
    given.

    Attributes:
        rules: Names of the rules to apply, drawn from RULES. Empty turns filtering off.
        max_depth_m: 'deep' fires where the shallowest reference cell within
            ref_window_m is deeper than this.
        ref_window_m: Radius, in metres, of the window 'deep' searches.
        near_surface_m: 'offshore' fires on photons less than this far below the
            EGM2008 geoid (a proxy for the sea surface).
        min_coast_dist_m: ...and more than this far from the coastline.
        ref_tolerance_m: 'reference' fires on photons shallower than the reference
            bathymetry by more than this.
        land_max_below_sl_m: 'land' fires on photons inside the landmask and more than
            this far below the EGM2008 geoid.
        offshore_min_ref_depth_m: If set, 'offshore' fires only where the reference is
            also deeper than this, which protects shallow banks far from any coast.
        ref_raster: Reference bathymetry raster (heights in metres, positive up,
            relative to EGM2008). None uses ETOPO 2022.

    """

    rules: tuple[str, ...] = RULES
    max_depth_m: float = 30.0
    ref_window_m: float = 1000.0
    near_surface_m: float = 5.0
    min_coast_dist_m: float = 500.0
    ref_tolerance_m: float = 30.0
    land_max_below_sl_m: float = 10.0
    offshore_min_ref_depth_m: float | None = None
    ref_raster: str | None = None

    def __post_init__(self) -> None:
        """Normalize the rules and check every value."""
        object.__setattr__(self, "rules", parse_rules(self.rules))
        for name in (
            "max_depth_m",
            "ref_window_m",
            "near_surface_m",
            "min_coast_dist_m",
            "ref_tolerance_m",
            "land_max_below_sl_m",
            "offshore_min_ref_depth_m",
        ):
            value = getattr(self, name)
            if value is None and name == "offshore_min_ref_depth_m":
                continue
            value = float(value)
            if not math.isfinite(value) or value < 0:
                msg = f"Bathymetry filter setting '{name}' must be a number >= 0, got {value!r}."
                raise ValueError(msg)
            object.__setattr__(self, name, value)
        if self.ref_raster is not None:
            object.__setattr__(self, "ref_raster", os.path.abspath(self.ref_raster))

    @classmethod
    def from_config(cls, config=None, **overrides: object) -> Self:
        """Build settings from the IVERT config, with any non-None override on top.

        Args:
            config: an ivert.utils.configfile.Config. Defaults to a freshly read one.
            **overrides: field values that replace the config's. None means "use the
                config value". For offshore_min_ref_depth_m and ref_raster, the string
                'none' (or an empty string) means "off" / "use ETOPO".

        Returns:
            A BathyFilterSettings.

        """
        if config is None:
            import ivert.utils.configfile

            config = ivert.utils.configfile.Config()

        config_keys = {
            "rules": "bathy_filters",
            "max_depth_m": "bathy_max_depth_m",
            "ref_window_m": "bathy_ref_window_m",
            "near_surface_m": "bathy_near_surface_m",
            "min_coast_dist_m": "bathy_min_coast_dist_m",
            "ref_tolerance_m": "bathy_ref_tolerance_m",
            "land_max_below_sl_m": "bathy_land_max_below_sl_m",
            "offshore_min_ref_depth_m": "bathy_offshore_min_ref_depth_m",
            "ref_raster": "bathy_ref_raster",
        }
        unknown = set(overrides) - set(config_keys)
        if unknown:
            msg = f"Unknown bathymetry filter setting(s): {', '.join(sorted(unknown))}."
            raise TypeError(msg)

        defaults = cls()
        values = {}
        for field, key in config_keys.items():
            value = overrides.get(field)
            if value is None:
                value = getattr(config, key, getattr(defaults, field))
            values[field] = value

        for field in ("offshore_min_ref_depth_m", "ref_raster"):
            if isinstance(values[field], str) and values[field].strip().lower() in (
                "",
                "none",
            ):
                values[field] = None
        return cls(**values)

    def rule_descriptions(self) -> dict[str, str]:
        """One phrase per enabled rule saying which photons it removes."""
        guard = (
            f", where the reference is deeper than {self.offshore_min_ref_depth_m:g} m"
            if self.offshore_min_ref_depth_m is not None
            else ""
        )
        text = {
            "deep": (
                f"where the shallowest reference cell within {self.ref_window_m:g} m is "
                f"deeper than {self.max_depth_m:g} m"
            ),
            "offshore": (
                f"outside the landmask, less than {self.near_surface_m:g} m below the "
                f"EGM2008 geoid, and more than {self.min_coast_dist_m:g} m from the "
                f"coastline{guard}"
            ),
            "reference": (
                f"more than {self.ref_tolerance_m:g} m shallower than the reference"
            ),
            "land": (
                f"inside the landmask and more than {self.land_max_below_sl_m:g} m "
                "below the EGM2008 geoid"
            ),
        }
        return {rule: text[rule] for rule in self.rules}

    @property
    def reference_description(self) -> str:
        """The reference bathymetry these settings use, for reports."""
        return self.ref_raster or ETOPO_DESCRIPTION

    @property
    def needs_reference(self) -> bool:
        """Whether any enabled rule reads the reference bathymetry."""
        return (
            "deep" in self.rules
            or "reference" in self.rules
            or ("offshore" in self.rules and self.offshore_min_ref_depth_m is not None)
        )

    @property
    def needs_landmask(self) -> bool:
        """Whether any enabled rule reads the landmask."""
        return "offshore" in self.rules or "land" in self.rules


def parse_rules(value) -> tuple[str, ...]:
    """Turn a rule list from the config or command line into a tuple of rule names.

    Accepts a comma-separated string, a list or tuple, or anything meaning "none"
    (None, False, "", "none", "off"). Names are returned in RULES order.

    Raises:
        ValueError: if a name is not one of RULES.

    """
    if value is None or value is False:
        return ()
    names = value.split(",") if isinstance(value, str) else list(value)
    names = {str(n).strip().lower() for n in names} - {""}
    if names <= {"none", "off", "false"}:
        return ()
    unknown = names - set(RULES)
    if unknown:
        msg = (
            f"Unknown bathymetry filter(s): {', '.join(sorted(unknown))}. "
            f"Choose from {', '.join(RULES)}, or 'none'."
        )
        raise ValueError(msg)
    return tuple(r for r in RULES if r in names)


@dataclasses.dataclass
class BathyFilterReport:
    """What the bathymetry filters did to one validation (or several, combined).

    Attributes:
        settings: the settings the filters ran with.
        n_bathy_photons: class-40 photons seen before filtering.
        removed: photons each rule flagged. A photon failing several rules counts
            under each.
        removed_total: photons removed, each counted once.
        n_validations: how many validations were combined into this report.

    """

    settings: BathyFilterSettings
    n_bathy_photons: int = 0
    removed: dict = dataclasses.field(default_factory=dict)
    removed_total: int = 0
    n_validations: int = 1

    def to_json(self) -> str:
        """Serialize, for storing on a results file."""
        return json.dumps(
            {
                "settings": dataclasses.asdict(self.settings),
                "n_bathy_photons": int(self.n_bathy_photons),
                "removed": {k: int(v) for k, v in self.removed.items()},
                "removed_total": int(self.removed_total),
                "n_validations": int(self.n_validations),
            },
        )

    @classmethod
    def from_json(cls, text: str) -> Self:
        """The inverse of to_json()."""
        d = json.loads(text)
        d["settings"] = BathyFilterSettings(**d["settings"])
        return cls(**d)

    @classmethod
    def combine(cls, reports) -> Self | None:
        """Sum several reports that ran with the same settings.

        Returns None if there are none. If the settings differ, the counts are still
        summed but the first report's settings are kept, and a warning is logged.
        """
        reports = [r for r in reports if r is not None]
        if not reports:
            return None
        settings = reports[0].settings
        if any(r.settings != settings for r in reports[1:]):
            logger.warning(
                "The combined results were validated with different bathymetry filter "
                "settings; the summary lists only the first DEM's.",
            )
        removed = {}
        for r in reports:
            for k, v in r.removed.items():
                removed[k] = removed.get(k, 0) + v
        return cls(
            settings=settings,
            n_bathy_photons=sum(r.n_bathy_photons for r in reports),
            removed=removed,
            removed_total=sum(r.removed_total for r in reports),
            n_validations=sum(r.n_validations for r in reports),
        )

    def summary_lines(self) -> list[str]:
        """Lines for the summary stats file."""
        s = self.settings
        lines = ["== Bathymetry photon filters (class 40) ==="]
        if not s.rules:
            lines.append("    Rules applied: none")
            return lines
        lines.append(f"    Rules applied: {', '.join(s.rules)}")
        lines.append(f"    Reference bathymetry: {s.reference_description}")
        lines.extend(
            f"    '{rule}' removes photons {text}"
            for rule, text in s.rule_descriptions().items()
        )
        scope = (
            f", summed over {self.n_validations} DEMs" if self.n_validations > 1 else ""
        )
        lines.append(
            f"    Bathymetry photons before filtering (photons{scope}): "
            f"{self.n_bathy_photons}",
        )
        lines.extend(
            f"    Removed by '{rule}' (photons): {self.removed.get(rule, 0)}"
            for rule in s.rules
        )
        lines.append(
            "    Removed in total, each photon counted once (photons): "
            f"{self.removed_total}",
        )
        return lines

    def log_line(self) -> str:
        """A one-line account of the counts, for the log."""
        per_rule = ", ".join(
            f"{rule}: {self.removed.get(rule, 0):,}" for rule in self.settings.rules
        )
        return (
            f"Bathymetry filters removed {self.removed_total:,} of "
            f"{self.n_bathy_photons:,} class-40 photons ({per_rule})."
        )


def write_report_to_h5(h5_file: str, report) -> None:
    """Store a BathyFilterReport as an attribute of the (only) table in an .h5 file.

    Does nothing if report is None. An attribute rather than a second table, so
    pd.read_hdf() on the file still needs no key.
    """
    if report is None:
        return
    with pd.HDFStore(h5_file, mode="a") as store:
        setattr(
            store.get_storer(store.keys()[0]).attrs,
            H5_REPORT_ATTR,
            report.to_json(),
        )


def read_report_from_h5(h5_file: str):
    """The BathyFilterReport stored on a results .h5 file, or None if it has none."""
    if not h5_file or not os.path.exists(h5_file):
        return None
    try:
        with pd.HDFStore(h5_file, mode="r") as store:
            keys = store.keys()
            if not keys:
                return None
            attrs = store.get_storer(keys[0]).attrs
            text = getattr(attrs, H5_REPORT_ATTR, None)
    except (OSError, ValueError, KeyError):
        return None
    return BathyFilterReport.from_json(text) if text else None


def apply_bathy_filters(
    photon_df: pd.DataFrame,
    settings: BathyFilterSettings,
    photon_src_epsg: str = "EPSG:4326+4979",
    cache_dir: str | None = None,
    landmask_store_dir: str | None = None,
    osm_cache_dir: str | None = None,
    database_tiles=(),
):
    """Drop the class-40 photons that fail any of the enabled rules.

    Args:
        photon_df: photons with 'x' (longitude), 'y' (latitude), 'z' and 'class_code'
            columns, as the IVERT database returns them.
        settings: the rules and thresholds to apply.
        photon_src_epsg: compound CRS of the photon coordinates, e.g. 'EPSG:4326+4979'.
        cache_dir: where the reference bathymetry and vertical-shift grids are cached.
        landmask_store_dir: IVERT's landmask store (the 'ivert_landmask_directory'
            setting). See ivert.landmask.
        osm_cache_dir: where globato and fetchez cache OSM landmasks (IVERT's
            'icesat2_download_directory').
        database_tiles: the photon database's storage tiles, (xmin, xmax, ymin,
            ymax, ...), the unit in which missing landmasks are stored.

    Returns:
        (filtered photon_df, BathyFilterReport)

    """
    is_bathy = (photon_df["class_code"] == BATHY_FLOOR_CLASS).to_numpy()
    n_bathy = int(np.count_nonzero(is_bathy))
    report = BathyFilterReport(
        settings=settings,
        n_bathy_photons=n_bathy,
        removed=dict.fromkeys(settings.rules, 0),
    )
    if not settings.rules or n_bathy == 0:
        return photon_df, report

    bathy = photon_df.loc[is_bathy]
    lon, lat = _to_lonlat(
        bathy["x"].to_numpy(dtype=float),
        bathy["y"].to_numpy(dtype=float),
        photon_src_epsg,
    )
    ortho_h = _to_egm2008(
        lon,
        lat,
        bathy["z"].to_numpy(dtype=float),
        photon_src_epsg,
        cache_dir,
    )

    def get_landmask(bounds):
        west, south, east, north = bounds
        return ivert.landmask.load_landmask(
            (west, east, south, north),
            landmask_store_dir,
            osm_cache_dir,
            database_tiles=database_tiles,
        )

    flags = _evaluate_rules(lon, lat, ortho_h, settings, cache_dir, get_landmask)

    fails = np.zeros(n_bathy, dtype=bool)
    for rule, mask in flags.items():
        report.removed[rule] = int(np.count_nonzero(mask))
        fails |= mask
    report.removed_total = int(np.count_nonzero(fails))

    logger.info(report.log_line())

    drop = np.zeros(len(photon_df), dtype=bool)
    drop[np.flatnonzero(is_bathy)[fails]] = True
    return photon_df.loc[~drop], report


def _evaluate_rules(lon, lat, ortho_h, settings, cache_dir, get_landmask):
    """Flag, per enabled rule, the photons that fail it.

    get_landmask(bounds) returns (land, covered area, coastline) over a
    (west, south, east, north) box.
    """
    rules = settings.rules
    n = len(lon)
    no = np.zeros(n, dtype=bool)

    # Bounds wide enough that every window and coast distance measured from a
    # photon stays inside the data read.
    buffer_m = 2.0 * max(settings.ref_window_m, settings.min_coast_dist_m, 1000.0)
    bounds = _buffered_bounds(lon, lat, buffer_m)

    ref = ref_shallowest = None
    if settings.needs_reference:
        ref_array, ref_transform = _load_reference(
            settings.ref_raster,
            bounds,
            cache_dir,
        )
        ref = _sample(ref_array, ref_transform, lon, lat)
        if "deep" in rules:
            shallow_array, shallow_transform = _shallowest_within(
                ref_array,
                ref_transform,
                settings.ref_window_m,
                lat0=float(np.mean(lat)),
            )
            ref_shallowest = _sample(shallow_array, shallow_transform, lon, lat)

    on_land = covered = None
    coast = None
    if settings.needs_landmask:
        land, covered_area, coast = get_landmask(bounds)
        covered = shapely.intersects_xy(covered_area, lon, lat)
        on_land = covered & shapely.intersects_xy(land, lon, lat)
        n_uncovered = int(np.count_nonzero(~covered))
        if n_uncovered:
            logger.warning(
                "%s class-40 photons lie where no OSM landmask is available; the "
                "'offshore' and 'land' bathymetry filters skip them.",
                f"{n_uncovered:,}",
            )

    flags = {}
    with np.errstate(invalid="ignore"):
        if "deep" in rules:
            flags["deep"] = ref_shallowest < -settings.max_depth_m

        if "offshore" in rules:
            # Depth below the sea surface, approximated as depth below the EGM2008
            # geoid. That misses the ocean tide, dynamic topography and surge, which
            # can be several metres in macrotidal areas. Issue #112 tracks storing the
            # real instantaneous sea surface per photon to replace this proxy.
            depth_below_surface = -ortho_h
            candidate = (
                covered & ~on_land & (depth_below_surface < settings.near_surface_m)
            )
            if settings.offshore_min_ref_depth_m is not None:
                candidate &= ref < -settings.offshore_min_ref_depth_m
            offshore = no.copy()
            if candidate.any():
                dist = _distance_to_coast_m(coast, lon[candidate], lat[candidate])
                offshore[candidate] = dist > settings.min_coast_dist_m
            flags["offshore"] = offshore

        if "reference" in rules:
            flags["reference"] = (ortho_h - ref) > settings.ref_tolerance_m

        if "land" in rules:
            flags["land"] = on_land & (ortho_h < -settings.land_max_below_sl_m)

    # NaN comparisons are already False; make every mask a plain bool array.
    return {rule: np.asarray(flags[rule], dtype=bool) for rule in rules}


def _to_lonlat(x, y, photon_src_epsg):
    """Photon x/y as WGS84 longitude/latitude."""
    horz = str(photon_src_epsg).split("+", maxsplit=1)[0]
    crs = pyproj.CRS.from_user_input(horz)
    if crs.equals(pyproj.CRS.from_epsg(4326)):
        return x, y
    transformer = pyproj.Transformer.from_crs(crs, 4326, always_xy=True)
    return transformer.transform(x, y)


def _to_egm2008(lon, lat, z, photon_src_epsg, cache_dir):
    """Photon heights as EGM2008 orthometric heights."""
    src = str(photon_src_epsg)
    vert = src.rsplit("+", maxsplit=1)[-1] if "+" in src else "4979"
    vert = vert.rsplit(":", maxsplit=1)[-1]
    if vert == "3855":
        return z
    return np.asarray(
        ivert.transform_points._apply_vertical_transform(
            lon,
            lat,
            z,
            src_vert_epsg=vert,
            dst_vert_epsg="3855",
            src_region=None,
            cache_dir=cache_dir,
        ),
        dtype=float,
    )


def _buffered_bounds(lon, lat, buffer_m):
    """(west, south, east, north) of the points, buffered by buffer_m metres."""
    south, north = float(np.min(lat)), float(np.max(lat))
    coslat = max(math.cos(math.radians(max(abs(south), abs(north)))), 0.01)
    dlat = buffer_m / _M_PER_DEG
    dlon = buffer_m / (_M_PER_DEG * coslat)
    return (
        max(float(np.min(lon)) - dlon, -180.0),
        max(south - dlat, -90.0),
        min(float(np.max(lon)) + dlon, 180.0),
        min(north + dlat, 90.0),
    )


def _load_reference(ref_raster, bounds, cache_dir):
    """Read the reference bathymetry over bounds as (float64 array, transform).

    Nodata becomes NaN. Uses ETOPO 2022 15" surface tiles, fetched into cache_dir,
    unless ref_raster names a raster to use instead.
    """
    west, south, east, north = bounds
    if ref_raster:
        paths = [ref_raster]
    else:
        import fetchez

        with ivert.utils.logging_config.keep_root_logging():
            paths = fetchez.get(
                "etopo",
                region=[west, east, south, north],
                outdir=cache_dir,
                datatype="surface",
                resolution="15s",
                verbose=False,
            )
        paths = [
            p for p in paths or [] if str(p).endswith(".tif") and os.path.exists(p)
        ]
        if not paths:
            msg = (
                "Could not fetch the ETOPO 2022 reference bathymetry for the "
                f"bathymetry filters over {bounds}. Rerun with a network connection, "
                "or turn off the 'deep' and 'reference' filters."
            )
            raise RuntimeError(msg)

    # A projected raster is read through a lon/lat warp, so every raster lines up
    # with the photons' coordinates and the window arithmetic below.
    sources, opened = [], []
    try:
        for p in paths:
            ds = rasterio.open(p)
            opened.append(ds)
            if ds.crs is not None and not ds.crs.is_geographic:
                ds = rasterio.vrt.WarpedVRT(
                    ds,
                    crs="EPSG:4326",
                    resampling=rasterio.enums.Resampling.bilinear,
                )
                opened.append(ds)
            sources.append(ds)
        array, transform = rasterio.merge.merge(
            sources,
            bounds=(west, south, east, north),
            nodata=np.nan,
            dtype="float64",
        )
    finally:
        for ds in reversed(opened):
            ds.close()
    return array[0], transform


def _shallowest_within(array, transform, radius_m, lat0):
    """For each cell, the highest (shallowest) value within radius_m metres.

    Returns (array, transform). A raster fine enough that the window would span more
    than _MAX_WINDOW_CELLS cells is first reduced by block maximum, which can only
    widen the window slightly and so errs toward keeping photons.
    """
    res_x, res_y = abs(transform.a), abs(transform.e)
    m_per_cell_y = _M_PER_DEG * res_y
    m_per_cell_x = _M_PER_DEG * math.cos(math.radians(lat0)) * res_x
    factor = max(
        1,
        math.ceil(2 * radius_m / (min(m_per_cell_x, m_per_cell_y) * _MAX_WINDOW_CELLS)),
    )
    work = np.where(np.isnan(array), -np.inf, array)
    if factor > 1:
        rows = math.ceil(work.shape[0] / factor) * factor
        cols = math.ceil(work.shape[1] / factor) * factor
        padded = np.full((rows, cols), -np.inf)
        padded[: work.shape[0], : work.shape[1]] = work
        work = padded.reshape(rows // factor, factor, cols // factor, factor).max(
            axis=(1, 3),
        )
        transform = transform * rasterio.transform.Affine.scale(factor)
        m_per_cell_x *= factor
        m_per_cell_y *= factor

    ry = math.ceil(radius_m / m_per_cell_y)
    rx = math.ceil(radius_m / m_per_cell_x)
    yy, xx = np.mgrid[-ry : ry + 1, -rx : rx + 1]
    footprint = (yy * m_per_cell_y) ** 2 + (xx * m_per_cell_x) ** 2 <= radius_m**2
    out = ndimage.maximum_filter(work, footprint=footprint, mode="nearest")
    out[np.isneginf(out)] = np.nan
    return out, transform


def _sample(array, transform, lon, lat):
    """Values of array at the points; NaN outside it."""
    rows, cols = rasterio.transform.rowcol(transform, lon, lat)
    rows, cols = np.asarray(rows), np.asarray(cols)
    inside = (
        (rows >= 0) & (rows < array.shape[0]) & (cols >= 0) & (cols < array.shape[1])
    )
    out = np.full(len(rows), np.nan)
    out[inside] = array[rows[inside], cols[inside]]
    return out


def _distance_to_coast_m(coast, lon, lat):
    """Distance in metres from each point to the nearest coastline (inf if none)."""
    if coast is None or coast.is_empty:
        return np.full(len(lon), np.inf)
    lon0 = float(np.mean(lon))
    lat0 = float(np.mean(lat))
    local = pyproj.Transformer.from_crs(
        4326,
        pyproj.CRS.from_proj4(
            f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m",
        ),
        always_xy=True,
    )
    coast_m = shapely.transform(
        coast,
        lambda xy: np.column_stack(local.transform(xy[:, 0], xy[:, 1])),
    )
    coast_points = shapely.get_coordinates(
        shapely.segmentize(coast_m, _COAST_POINT_SPACING_M),
    )
    px, py = local.transform(lon, lat)
    dist, _ = cKDTree(coast_points).query(np.column_stack([px, py]))
    return dist
