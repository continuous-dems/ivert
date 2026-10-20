"""Plot classified ICESat-2 photon curtains from .nc granule files.

Usage
-----
    python plot_photon_clouds_v2.py <nc_file> [options]

The script reads a granule .nc file produced by IS2Database._process_h5_to_nc().
If the matching ATL03 .h5 file is available (searched in the same directory and
in the ivert cache), it splits photons by beam and plots one curtain per beam.
Without the .h5, all photons are plotted together sorted by latitude.

Photon class codes and their meanings come from globato's ATL03 reader; run
'ivert classes' (or see ivert.photon_classes) for the authoritative list.
"""

import configparser
import logging
import sys
import typing
from collections.abc import Iterable, Sequence
from pathlib import Path

import click
import matplotlib as mpl
import numpy as np
import pandas as pd

mpl.use("Agg")
import h5py
import matplotlib.pyplot as plt
import netCDF4

import ivert.utils.configfile
import ivert.vdatum_lookup
from ivert.photon_classes import class_labels
from ivert.utils.paths import absolute_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
# Visual attributes only; legend labels come from ivert.photon_classes so they
# stay in sync with the globato classifier.
CLASS_STYLE: dict[int, dict[str, typing.Any]] = {
    0: {"color": "grey", "zorder": 0.5, "alpha": 0.5, "s": 1},
    1: {"color": "saddlebrown", "zorder": 2, "alpha": 1.0, "s": 3},
    2: {"color": "limegreen", "zorder": 1, "alpha": 0.8, "s": 2},
    3: {"color": "forestgreen", "zorder": 2, "alpha": 0.9, "s": 2},
    7: {"color": "red", "zorder": 2, "alpha": 0.8, "s": 2},
    40: {"color": "darkorange", "zorder": 3, "alpha": 1.0, "s": 3},
    41: {"color": "dodgerblue", "zorder": 1, "alpha": 0.6, "s": 1},
    42: {"color": "dodgerblue", "zorder": 1, "alpha": 0.6, "s": 1},
}
DEFAULT_STYLE: dict[str, typing.Any] = {
    "color": "lightgrey",
    "zorder": 0,
    "alpha": 0.3,
    "s": 1,
}

# Photons further than this from sea level, in meters, are bad values and not plotted.
_MAX_ABS_ELEVATION_M = 1e5

DEM_COLORS = ["dimgrey", "purple", "darkcyan", "darkmagenta", "darkgoldenrod"]

# A DEM sampled along a track: (along-track km, elevations, label).
_DemProfile = tuple[np.ndarray, np.ndarray, str]
# Plot limits, (low, high), either of which may be left open.
_Limits = tuple[float | None, float | None]


def _granule_id(filepath: str | Path) -> str:
    """Return a file path's bare granule ID, without _subsetted or bbox suffixes."""
    stem = Path(filepath).stem
    for marker in ("_subsetted", "_W", "_N", "_E", "_S"):
        idx = stem.find(marker)
        if idx >= 0:
            stem = stem[:idx]
    return stem


def _h5_search_dirs(config: ivert.utils.configfile.Config) -> list[Path]:
    """Return the configured folders that may hold downloaded ATL03 .h5 files.

    The ICESat-2 download folder comes first, then the cache folder, which holds it
    by default; a folder named by both is listed once.
    """
    dirs = []
    for setting in ("icesat2_download_directory", "cache_directory"):
        value = getattr(config, setting, None)
        if value and Path(value) not in dirs:
            dirs.append(Path(value))
    return dirs


def _find_h5(nc_path: Path, cache_dirs: Iterable[str | Path] = ()) -> Path | None:
    """Search for a matching ATL03 .h5 whose name starts with the same granule ID.

    The .nc file's own folder is searched first, then each of cache_dirs, all with
    their subfolders.
    """
    gid = _granule_id(nc_path)
    if not gid:
        return None
    search_dirs = [Path(nc_path).parent, *(Path(d) for d in cache_dirs)]
    for d in search_dirs:
        hits = sorted(d.rglob(f"{gid}*.h5"))
        if hits:
            return hits[0]
    return None


def _beam_delta_times(h5_path: Path) -> dict[str, np.ndarray]:
    """Return {beam_name: delta_time_array} for all beams present in the .h5."""
    beams = {}
    with h5py.File(h5_path, "r") as f:
        for beam in ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]:
            try:
                dt = f[f"{beam}/heights/delta_time"][...]
                beams[beam] = dt
            except KeyError:
                pass
    return beams


def _load_h5_beam_photons(h5_path: Path, beam: str) -> pd.DataFrame:
    """Load all of one beam's photons from an ATL03 .h5, with class_code 0.

    Heights are converted from ellipsoidal to EGM2008 geoid by subtracting the
    geoid undulation interpolated from geophys_corr/geoid.  Cumulative along-track
    distance (along_track_m) is computed from geolocation/segment_length and
    heights/dist_ph_along, matching the convention used in the .nc files.
    """
    with h5py.File(h5_path, "r") as f:
        try:
            delta_time = f[f"{beam}/heights/delta_time"][...]
            lon = f[f"{beam}/heights/lon_ph"][...]
            lat = f[f"{beam}/heights/lat_ph"][...]
            h_ph = f[f"{beam}/heights/h_ph"][...]
            dist_ph_along = f[f"{beam}/heights/dist_ph_along"][...]
            geoid_dt = f[f"{beam}/geophys_corr/delta_time"][...]
            geoid = f[f"{beam}/geophys_corr/geoid"][...]
            ph_index_beg = f[f"{beam}/geolocation/ph_index_beg"][...]
            seg_length = f[f"{beam}/geolocation/segment_length"][...]
        except KeyError:
            return pd.DataFrame(
                columns=["x", "y", "z", "delta_time", "class_code", "along_track_m"],
            )

    geoid_ph = np.interp(delta_time, geoid_dt, geoid)
    z = h_ph - geoid_ph

    n = len(delta_time)
    seg_cumul_start = np.concatenate([[0.0], np.cumsum(seg_length[:-1])])
    # ph_index_beg is the 1-based heights row of a segment's first photon, and
    # 0 for a segment with no photons, so only the non-empty segments are in
    # order and searchable; the photon's own row is likewise 1-based here.
    has_photons = ph_index_beg > 0
    seg_of_ph = np.clip(
        np.searchsorted(ph_index_beg[has_photons], np.arange(1, n + 1), side="right")
        - 1,
        0,
        int(has_photons.sum()) - 1,
    )
    along_track_m = seg_cumul_start[has_photons][seg_of_ph] + dist_ph_along

    order = np.argsort(delta_time)
    delta_time = delta_time[order]
    lon = lon[order]
    lat = lat[order]
    z = z[order]
    along_track_m = along_track_m[order]

    return pd.DataFrame(
        {
            "x": lon,
            "y": lat,
            "z": z,
            "delta_time": delta_time,
            "class_code": np.zeros(n, dtype=np.int8),
            "along_track_m": along_track_m,
        },
    )


def load_nc(nc_path: str | Path) -> pd.DataFrame:
    """Load the .nc granule into a DataFrame."""
    data = {}
    with netCDF4.Dataset(nc_path) as ds:
        for v in ds.variables:
            raw = ds.variables[v][:]
            arr = np.asarray(raw.data if hasattr(raw, "data") else raw)
            if arr.dtype.kind == "O":
                arr = arr.astype(str)
            elif arr.dtype.kind == "S" and arr.ndim > 1:
                # A fixed-width string variable such as 'laser' (char laser(index,
                # string4)) comes back one character per column; join each row
                # into one string ('gt1l') so it fits a DataFrame column.
                arr = netCDF4.chartostring(arr)
            data[v] = arr
    return pd.DataFrame(data)


def _get_vdatum_label(reference: str) -> str:
    """Return a short human-readable label for a vertical reference.

    The reference is a string such as 'EPSG:5703' or 'vdatum:mllw'.
    """
    desc = ivert.vdatum_lookup.describe_vdatum(reference)
    if desc:
        return desc.replace(" height", "").replace(" Height", "")
    return reference


def _apply_vdatum_to_df(
    df: pd.DataFrame,
    target_vert: str,
    cache_dir: str | None = None,
) -> pd.DataFrame:
    """Return a copy of df with z transformed from EGM2008 to target vertical datum.

    'target_vert' is a bare EPSG code ('5703') or a transformez reference ID
    ('vdatum:mllw').
    """
    import ivert.transform_points as tp  # noqa: PLC0415 - slow import

    src = "EPSG:4326+3855"
    dst = f"EPSG:4326+{target_vert}"
    try:
        _, _, z_new = tp.transform_points(
            df["x"].values,
            df["y"].values,
            df["z"].values,
            src_epsg=src,
            dst_epsg=dst,
            cache_dir=cache_dir,
        )
        df = df.copy()
        df["z"] = z_new
    # transform_points can fetch datum grids over the network, so failures are
    # open-ended; whatever goes wrong, plotting in EGM2008 is still useful.
    except Exception as e:  # noqa: BLE001
        logger.warning("vdatum transform failed (%s). Plotting in EGM2008.", e)
    return df


def _sample_dem_along_track(
    dem_path: str,
    lons: np.ndarray,
    lats: np.ndarray,
    along_track_m: np.ndarray,
    *,
    target_vert: str | None = None,
    cache_dir: str | None = None,
) -> _DemProfile | None:
    """Sample a DEM raster along a laser track at the DEM's native pixel resolution.

    Rather than sampling only at photon locations (which leaves flat gaps between
    clusters), this interpolates the track to an evenly-spaced grid at approximately
    the DEM's pixel size, producing a continuous profile.  Individual DEM grid cells
    may be sampled more than once where the track runs at a shallow angle.

    Returns (along_track_km, z_dem, label) or None if the DEM has no overlap.
    When target_vert is given and differs from the DEM's native vertical datum,
    the sampled elevations are transformed to that datum.
    """
    import pyproj  # noqa: PLC0415 - slow import
    import rasterio  # noqa: PLC0415 - slow import

    lons = np.asarray(lons, dtype=float)
    lats = np.asarray(lats, dtype=float)
    along_track_m = np.asarray(along_track_m, dtype=float)

    # Sort by along-track distance so np.interp works correctly.
    order = np.argsort(along_track_m)
    lons, lats, along_track_m = lons[order], lats[order], along_track_m[order]

    try:
        with rasterio.open(dem_path) as src:
            dem_nodata = src.nodata
            dem_rc_crs = src.crs

            # Estimate the DEM pixel size in metres along-track.
            res_crs_x, res_crs_y = src.res  # native CRS units
            if dem_rc_crs is not None:
                dem_py_crs = pyproj.CRS.from_user_input(dem_rc_crs.to_wkt())
                if dem_py_crs.is_geographic:
                    clat = float(np.mean(lats))
                    res_m = min(
                        res_crs_x * 111320.0 * np.cos(np.radians(clat)),
                        res_crs_y * 111320.0,
                    )
                else:
                    res_m = min(res_crs_x, res_crs_y)
            else:
                res_m = min(res_crs_x, res_crs_y)
            res_m = max(res_m, 1.0)  # guard against zero or sub-metre values

            # Build a dense along-track grid at DEM resolution spacing.
            atm_min, atm_max = along_track_m[0], along_track_m[-1]
            n_pts = max(2, int(np.ceil((atm_max - atm_min) / res_m)) + 1)
            dense_atm = np.linspace(atm_min, atm_max, n_pts)

            # Interpolate lon/lat onto the dense grid.
            dense_lons = np.interp(dense_atm, along_track_m, lons)
            dense_lats = np.interp(dense_atm, along_track_m, lats)

            # Reproject to DEM CRS and sample.
            if dem_rc_crs is not None:
                xformer = pyproj.Transformer.from_crs(
                    pyproj.CRS.from_epsg(4326),
                    dem_py_crs,
                    always_xy=True,
                )
                px, py = xformer.transform(dense_lons, dense_lats)
            else:
                px, py = dense_lons.copy(), dense_lats.copy()

            samples = list(src.sample(list(zip(px.tolist(), py.tolist(), strict=True))))
    except (
        OSError,
        ValueError,
        rasterio.errors.RasterioError,
        pyproj.exceptions.ProjError,
    ) as e:
        logger.warning("Could not sample DEM %s: %s", Path(dem_path).name, e)
        return None

    z_dem = np.array([s[0] if len(s) else np.nan for s in samples], dtype=float)

    if dem_nodata is not None:
        z_dem[np.isclose(z_dem, dem_nodata, rtol=0, atol=1e-3)] = np.nan
    valid = np.isfinite(z_dem)
    if not np.any(valid):
        logger.info(
            "  DEM %s: no overlap with laser track.",
            Path(dem_path).name,
        )
        return None

    if target_vert is not None:
        z_dem = _dem_profile_to_vert(
            dem_path,
            dense_lons,
            dense_lats,
            z_dem,
            target_vert=target_vert,
            cache_dir=cache_dir,
        )
        valid = np.isfinite(z_dem)

    sort_idx = np.argsort(dense_atm[valid])
    valid_idx = np.where(valid)[0][sort_idx]
    label = Path(dem_path).stem
    return dense_atm[valid_idx] / 1000.0, z_dem[valid_idx], label


def _dem_profile_to_vert(
    dem_path: str,
    lons: np.ndarray,
    lats: np.ndarray,
    z_dem: np.ndarray,
    *,
    target_vert: str,
    cache_dir: str | None = None,
) -> np.ndarray:
    """Return a DEM profile's finite elevations transformed to the 'target_vert' EPSG.

    The NaNs stay NaN. The profile comes back unchanged when the DEM's vertical
    datum can't be read, has no EPSG code, is already 'target_vert', or the
    transform fails.
    """
    import pyproj  # noqa: PLC0415 - slow import
    import rasterio  # noqa: PLC0415 - slow import

    import ivert.transform_points as tp  # noqa: PLC0415 - slow import
    from ivert.utils import dem_geom  # noqa: PLC0415 - slow import

    try:
        _, dem_vert = dem_geom.get_dem_reference_frame_from_file(dem_path)
    except (
        OSError,
        ValueError,
        rasterio.errors.RasterioError,
        pyproj.exceptions.ProjError,
    ):
        return z_dem
    if dem_vert is None:
        return z_dem
    dem_vert_epsg = dem_vert.to_epsg()
    if dem_vert_epsg is None or str(dem_vert_epsg) == target_vert:
        return z_dem

    valid = np.isfinite(z_dem)
    try:
        _, _, z_tx = tp.transform_points(
            lons[valid],
            lats[valid],
            z_dem[valid],
            src_epsg=f"EPSG:4326+{dem_vert_epsg}",
            dst_epsg=f"EPSG:4326+{target_vert}",
            cache_dir=cache_dir,
        )
    # As in _apply_vdatum_to_df: failures are open-ended, and the untransformed
    # profile is still worth plotting.
    except Exception as e:  # noqa: BLE001
        logger.warning("DEM vertical transform failed: %s", e)
        return z_dem
    z_out = np.full(len(z_dem), np.nan)
    z_out[valid] = z_tx
    return z_out


def _collect_dem_profiles(
    dem_paths: Sequence[str],
    lons: np.ndarray,
    lats: np.ndarray,
    along_track_m: np.ndarray,
    *,
    target_vert: str | None,
    cache_dir: str | None,
) -> list[_DemProfile]:
    """Sample each DEM and return a list of (along_km, z, label) profiles."""
    profiles = []
    for p in dem_paths:
        result = _sample_dem_along_track(
            p,
            lons,
            lats,
            along_track_m,
            target_vert=target_vert,
            cache_dir=cache_dir,
        )
        if result is not None:
            logger.info(
                "  DEM %s: %s sampled points",
                Path(p).name,
                f"{len(result[0]):,}",
            )
            profiles.append(result)
    return profiles


def _positions_for_dem_sampling(
    df: pd.DataFrame,
    dlim: _Limits | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lons, lats, along_m) restricted to the dlim window (km).

    When dlim is None or both bounds are None, the full arrays are returned.
    This ensures DEM sampling only covers the segment that will actually be plotted,
    so DEMs outside the window are skipped and don't appear in the legend.
    """
    atk_km = df["along_track_m"].to_numpy() / 1000.0
    lo = dlim[0] if (dlim is not None and dlim[0] is not None) else -np.inf
    hi = dlim[1] if (dlim is not None and dlim[1] is not None) else np.inf
    mask = (atk_km >= lo) & (atk_km <= hi)
    return (
        df["x"].to_numpy()[mask],
        df["y"].to_numpy()[mask],
        df["along_track_m"].to_numpy()[mask],
    )


def plot_beam(
    df_beam: pd.DataFrame,
    beam_name: str,
    outpath: str | Path,
    *,
    zlim: _Limits | None = None,
    dlim: _Limits | None = None,
    classes: set[int] | None = None,
    title_extra: str = "",
    dem_profiles: list[_DemProfile] | None = None,
    ylabel: str | None = None,
) -> None:
    """Plot one beam's photon curtain (along-track km vs elevation).

    classes: None  → plot all class codes present
             set() → reclassify all classified photons as noise (class 0)
             {1, 40, …} → plot those class codes; all others reclassified as noise
    """
    sort_col = "delta_time" if "delta_time" in df_beam.columns else "y"
    df_beam = df_beam.sort_values(sort_col).reset_index(drop=True)

    # Drop photons with non-physical elevations
    df_beam = df_beam[
        df_beam["z"].between(-_MAX_ABS_ELEVATION_M, _MAX_ABS_ELEVATION_M)
    ].reset_index(drop=True)

    # Reclassify photons not in the requested set to noise (class 0) so they still
    # appear
    if classes is not None:
        unselected = (df_beam["class_code"] != 0) & ~df_beam["class_code"].isin(classes)
        df_beam.loc[unselected, "class_code"] = 0

    along_track = df_beam["along_track_m"].to_numpy() / 1000.0
    z = df_beam["z"].to_numpy()
    cc = df_beam["class_code"].to_numpy()

    fig, ax = plt.subplots(figsize=(12, 4))

    labels = class_labels()
    for code in np.unique(cc):
        mask = cc == code
        style = CLASS_STYLE.get(int(code), DEFAULT_STYLE)
        label = labels.get(int(code), "Other")
        ax.scatter(
            along_track[mask],
            z[mask],
            c=style["color"],
            label=f"{label} (n={mask.sum():,})",
            zorder=style["zorder"],
            alpha=style["alpha"],
            s=style["s"],
            linewidths=0,
        )

    if dem_profiles:
        for i, (dem_atk, dem_z, dem_lbl) in enumerate(dem_profiles):
            color = DEM_COLORS[i % len(DEM_COLORS)]
            ax.plot(
                dem_atk,
                dem_z,
                color=color,
                linewidth=0.8,
                label=dem_lbl,
                zorder=0.75,
                alpha=0.75,
            )

    ax.set_xlabel("Along-track distance (km)")
    ax.set_ylabel(ylabel or "Elevation / depth (m, EGM2008 geoid)")
    title = f"{Path(outpath).stem}  —  {beam_name}"
    if title_extra:
        title += f"  {title_extra}"
    ax.set_title(title, fontsize=8)
    if zlim is not None:
        ax.set_ylim(bottom=zlim[0], top=zlim[1])
    if dlim is not None:
        ax.set_xlim(left=dlim[0], right=dlim[1])
    ax.legend(loc="upper right", fontsize=7, markerscale=2)
    ax.grid(visible=True, linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    fig.savefig(outpath, dpi=200)
    plt.close(fig)
    logger.info("  Saved %s", outpath)


class _PlotSettings(typing.NamedTuple):
    """How main() plots each beam: the limits, classes, DEMs and vertical datum."""

    zlim: _Limits | None
    dlim: _Limits | None
    classes: set[int] | None
    dem: Sequence[str]
    # The vertical datum to transform photons and DEMs to, or None for EGM2008.
    target_vert: str | None
    # Where datum-shift grids are cached, or None for the working directory.
    cache_dir: str | None
    ylabel: str


def _resolve_target_vdatum(vdatum: str | None) -> tuple[str | None, str]:
    """Return a --vdatum value's vertical datum and the plots' y-axis label.

    Without a datum the plots stay in EGM2008, untransformed. Exits with a message
    if the datum isn't recognised or can't be used.
    """
    if not vdatum:
        return None, "Elevation / depth (m, EGM2008 geoid)"

    from ivert.utils import dem_geom  # noqa: PLC0415 - slow import

    vdatum_str = ivert.vdatum_lookup.resolve_vdatum(vdatum)
    if vdatum_str is None:
        sys.exit(
            f"Unknown vertical datum: {vdatum!r}. "
            "Use an EPSG code or common name (e.g. 'navd88', 'egm2008', 'mllw').",
        )
    try:
        ivert.vdatum_lookup.check_vdatum(vdatum_str)
    except ValueError as exc:
        sys.exit(f"Vertical datum {vdatum!r} can't be used: {exc}")
    _, target_vert = dem_geom.split_srs_string(vdatum_str)
    return target_vert, f"Elevation / depth (m, {_get_vdatum_label(vdatum_str)})"


def _limits(low: float | None, high: float | None) -> _Limits | None:
    """Return plot limits, or None if neither end is set."""
    if low is None and high is None:
        return None
    return (low, high)


def _parse_classes(classes_str: str | None) -> set[int] | None:
    """Return the --classes codes to highlight: None for all, empty for none."""
    if classes_str is None:
        return None
    if classes_str == "":
        return set()
    return {int(c) for c in classes_str.split("/")}


def _h5_only_path(
    input_path: Path,
    h5_file: str | None,
    h5_search_dirs: Sequence[Path],
) -> Path:
    """Return the .h5 file to plot on its own: the input, --h5's path, or one found.

    Exits with a message if the file named doesn't exist or none is found.
    """
    if input_path.suffix.lower() == ".h5":
        return input_path
    if h5_file:
        h5_path = absolute_path(h5_file)
        if not h5_path.exists():
            sys.exit(f".h5 file not found: {h5_path}")
        return h5_path
    found = _find_h5(input_path, h5_search_dirs)
    if found is None:
        sys.exit("--h5-only: no matching .h5 found in cache.")
    return found


def _background_h5_path(
    nc_path: Path,
    h5_file: str | None,
    h5_search_dirs: Sequence[Path],
    *,
    search: bool,
) -> Path | None:
    """Return the .h5 file to plot under an .nc file's photons, or None.

    That is --h5's path, or with 'search' a file found for the granule (a warning
    if none is). Exits with a message if the file named doesn't exist.
    """
    if search:
        found = _find_h5(nc_path, h5_search_dirs)
        if found is None:
            logger.warning("--h5 given but no matching .h5 found in cache.")
        return found
    if h5_file:
        h5_path = absolute_path(h5_file)
        if not h5_path.exists():
            sys.exit(f".h5 file not found: {h5_path}")
        return h5_path
    return None


def _plot_with_dems(
    df_plot: pd.DataFrame,
    beam: str,
    outpath: Path,
    settings: _PlotSettings,
) -> None:
    """Plot one beam's photons, in the target datum, with the DEMs along its track."""
    if settings.target_vert:
        df_plot = _apply_vdatum_to_df(
            df_plot,
            settings.target_vert,
            settings.cache_dir,
        )
    lons, lats, along_track = _positions_for_dem_sampling(df_plot, settings.dlim)
    dem_profiles = _collect_dem_profiles(
        settings.dem,
        lons,
        lats,
        along_track,
        target_vert=settings.target_vert,
        cache_dir=settings.cache_dir,
    )
    plot_beam(
        df_plot,
        beam,
        outpath,
        zlim=settings.zlim,
        dlim=settings.dlim,
        classes=settings.classes,
        dem_profiles=dem_profiles or None,
        ylabel=settings.ylabel,
    )


def _plot_h5_only(
    h5_path: Path,
    laser: str | None,
    out_dir: Path,
    settings: _PlotSettings,
) -> None:
    """Plot each beam of an .h5 file (or just 'laser'), all its photons as noise."""
    logger.info("H5-only: %s", h5_path.name)
    beam_dts = _beam_delta_times(h5_path)
    beams_to_plot = [laser] if laser else list(beam_dts.keys())

    for beam in beams_to_plot:
        if beam not in beam_dts:
            logger.info("  Beam %s not in .h5, skipping.", beam)
            continue
        df_plot = _load_h5_beam_photons(h5_path, beam)
        if df_plot.empty:
            logger.info("  Beam %s: no photons, skipping.", beam)
            continue
        logger.info("  Beam %s: %s photons", beam, f"{len(df_plot):,}")
        _plot_with_dems(df_plot, beam, out_dir / f"{h5_path.stem}_{beam}.png", settings)


def _nc_beam_photons(
    df: pd.DataFrame,
    df_bg: pd.DataFrame,
    beam: str,
) -> pd.DataFrame:
    """Return an .nc file's photons of one beam.

    The beam is picked by the laser column, or in old .nc files without one by
    matching (delta_time, x, y) to the beam's .h5 photons, 'df_bg'.
    """
    if "laser" in df.columns:
        return df[df["laser"] == beam].copy()
    return df.merge(
        df_bg[["delta_time", "x", "y"]],
        on=["delta_time", "x", "y"],
        how="inner",
    )


def _with_along_track(df_beam: pd.DataFrame, df_bg: pd.DataFrame) -> pd.DataFrame:
    """Return a beam's .nc photons with their along-track distances.

    An .nc file from before the field was added gets them from the matching .h5
    photons, 'df_bg', by (delta_time, x, y); photons with no match are dropped.
    """
    if "along_track_m" in df_beam.columns:
        return df_beam
    return df_beam.merge(
        df_bg[["delta_time", "x", "y", "along_track_m"]],
        on=["delta_time", "x", "y"],
        how="left",
    ).dropna(subset=["along_track_m"])


def _plot_nc_with_h5(
    df: pd.DataFrame,
    h5_path: Path,
    laser: str | None,
    out_base: Path,
    settings: _PlotSettings,
) -> None:
    """Plot each beam's .nc photons over all the .h5 file's photons, as noise.

    Each plot is written to '<out_base>_<beam>.png'.
    """
    logger.info("Found .h5: %s", h5_path.name)
    beam_dts = _beam_delta_times(h5_path)
    beams_to_plot = [laser] if laser else list(beam_dts.keys())

    for beam in beams_to_plot:
        if beam not in beam_dts:
            logger.info("  Beam %s not in .h5, skipping.", beam)
            continue

        df_bg = _load_h5_beam_photons(h5_path, beam)
        if df_bg.empty:
            logger.info("  Beam %s: no h5 photons, skipping.", beam)
            continue

        df_beam = _nc_beam_photons(df, df_bg, beam)
        if df_beam.empty:
            logger.info("  Beam %s: no photons in .nc, skipping.", beam)
            continue
        df_beam = _with_along_track(df_beam, df_bg)

        logger.info(
            "  Beam %s: %s classified + %s background photons",
            beam,
            f"{len(df_beam):,}",
            f"{len(df_bg):,}",
        )
        df_plot = pd.concat([df_bg, df_beam], ignore_index=True)
        _plot_with_dems(df_plot, beam, Path(f"{out_base}_{beam}.png"), settings)


def _plot_nc_only(
    df: pd.DataFrame,
    laser: str | None,
    out_base: Path,
    settings: _PlotSettings,
) -> None:
    """Plot each beam of an .nc file's photons, from its laser and along-track fields.

    Each plot is written to '<out_base>_<beam>.png'.
    """
    beams_in_nc = sorted(df["laser"].unique())
    beams_to_plot = [laser] if laser else beams_in_nc
    for beam in beams_to_plot:
        df_beam = df[df["laser"] == beam].copy()
        if df_beam.empty:
            continue
        if "along_track_m" not in df_beam.columns:
            logger.info("  Beam %s: nc has no along_track_m, skipping.", beam)
            continue
        logger.info("  Beam %s: %s photons (nc only)", beam, f"{len(df_beam):,}")
        _plot_with_dems(df_beam, beam, Path(f"{out_base}_{beam}.png"), settings)


# Sentinel flag_value for --h5 given without a path.
_H5_SEARCH_CACHE = "__SEARCH_CACHE__"


@click.command(help="Plot classified ICESat-2 photon curtains.")
@click.argument("input_file", type=click.Path(path_type=Path))
@click.option(
    "--laser",
    "-b",
    default=None,
    help="Laser/beam to plot (e.g. gt2l). Default: plot all beams.",
)
@click.option(
    "--outdir",
    "-o",
    default=None,
    help="Output directory for images (default: same dir as input file).",
)
@click.option(
    "--zmin",
    type=float,
    default=None,
    help="Minimum elevation to display (m). Data outside [-1e5, 1e5] is "
    "always filtered regardless.",
)
@click.option(
    "--zmax",
    type=float,
    default=None,
    help="Maximum elevation to display (m).",
)
@click.option(
    "--xmin",
    type=float,
    default=None,
    help="Minimum along-track distance to display (km).",
)
@click.option(
    "--xmax",
    type=float,
    default=None,
    help="Maximum along-track distance to display (km).",
)
@click.option(
    "--classes",
    "classes_str",
    default=None,
    help="Slash-separated class codes to highlight (e.g. '1/40/41'). "
    "Photons not in the list are reclassified as noise and shown "
    "in grey. Default: show all classes. Pass '' to show all "
    "photons as noise.",
)
@click.option(
    "--h5",
    "h5_arg",
    is_flag=False,
    flag_value=_H5_SEARCH_CACHE,
    default=None,
    help="ATL03 .h5 file to use as noise background. Supply a path, or "
    "omit the path to search for a file whose name starts with the same "
    "granule ID as the .nc file, in the .nc file's folder and then in the "
    "icesat2_download_directory and cache_directory folders.",
)
@click.option(
    "--h5-only",
    is_flag=True,
    default=False,
    help="Plot only the ATL03 .h5 photons (all as noise); ignore the "
    ".nc classifications entirely. Automatically enabled when the "
    "input file is an .h5.",
)
@click.option(
    "--dem",
    multiple=True,
    metavar="DEM",
    help="A DEM raster file to profile along the laser track. May be given "
    "multiple times. Each overlapping DEM is plotted as a line at its "
    "sampled elevations.",
)
@click.option(
    "--vdatum",
    "-V",
    default=None,
    help="Target vertical datum for photons and DEMs (e.g. 'navd88', "
    "'egm2008', 'EPSG:5703'). Transforms ICESat-2 photons from "
    "EGM2008 and DEM elevations to the given datum so both are "
    "on the same vertical reference. Default: EGM2008 (no transform).",
)
def main(
    input_file: Path,
    *,
    laser: str | None,
    outdir: str | None,
    zmin: float | None,
    zmax: float | None,
    xmin: float | None,
    xmax: float | None,
    classes_str: str | None,
    h5_arg: str | None,
    h5_only: bool,
    dem: tuple[str, ...],
    vdatum: str | None,
) -> None:
    """Plot classified ICESat-2 photon curtains.

    INPUT_FILE is the path to the .nc granule file, or an ATL03 .h5 file
    (automatically enables --h5-only).
    """
    # --h5 is tri-state: the sentinel means "search the cache", a string is an
    # explicit path, None means the option was absent.
    search_h5 = h5_arg == _H5_SEARCH_CACHE
    h5_file = None if search_h5 else h5_arg
    # A str, not a click.Path, so that -o "" still means the input file's folder;
    # click.Path would make it ".".
    out_dir = Path(outdir) if outdir else None

    target_vert, ylabel = _resolve_target_vdatum(vdatum)

    # Datum-shift grid cache (use ivert cache if available, else cwd)
    try:
        config = ivert.utils.configfile.Config()
    except (configparser.Error, OSError):
        config = None
    cache_dir = config.cache_directory if config is not None else None
    h5_search_dirs = _h5_search_dirs(config) if config is not None else []

    input_path = absolute_path(input_file)
    if not input_path.exists():
        sys.exit(f"File not found: {input_path}")

    settings = _PlotSettings(
        zlim=_limits(zmin, zmax),
        dlim=_limits(xmin, xmax),
        classes=_parse_classes(classes_str),
        dem=dem,
        target_vert=target_vert,
        cache_dir=cache_dir,
        ylabel=ylabel,
    )

    if h5_only or input_path.suffix.lower() == ".h5":
        h5_path = _h5_only_path(input_path, h5_file, h5_search_dirs)
        out_dir = out_dir if out_dir is not None else h5_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        _plot_h5_only(h5_path, laser, out_dir, settings)
        return

    nc_path = input_path
    out_dir = out_dir if out_dir is not None else nc_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading %s ...", nc_path.name)
    df = load_nc(nc_path)

    # Resolve .h5 path for beam splitting and noise background
    background = _background_h5_path(
        nc_path,
        h5_file,
        h5_search_dirs,
        search=search_h5,
    )
    if background:
        _plot_nc_with_h5(df, background, laser, out_dir / nc_path.stem, settings)
    # No .h5 — use laser/along_track_m from the nc file directly if present.
    elif "laser" in df.columns:
        _plot_nc_only(df, laser, out_dir / nc_path.stem, settings)
    else:
        logger.info("No .h5 found and nc has no beam/distance info — cannot plot.")


if __name__ == "__main__":
    main()
