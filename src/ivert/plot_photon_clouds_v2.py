#!/usr/bin/env python3
"""plot_photon_clouds_v2.py — plot classified ICESat-2 photon curtains from .nc granule files.

Usage
-----
    python plot_photon_clouds_v2.py <nc_file> [options]

The script reads a granule .nc file produced by IS2Database._process_h5_to_nc().
If the matching ATL03 .h5 file is available (searched in the same directory and
in the ivert cache), it splits photons by beam and plots one curtain per beam.
Without the .h5, all photons are plotted together sorted by latitude.

Class codes (current convention):
     1 = ground/land
     2 = canopy
     3 = canopy top
     7 = built structure
    40 = bathy floor (seafloor)
    41 = bathy surface (water surface)
"""

import argparse
import os
import glob
import sys

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import netCDF4


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
CLASS_STYLE = {
     0: dict(color="grey",    label="Noise",          zorder=0.5, alpha=0.5, s=1),
     1: dict(color="saddlebrown",  label="Ground",         zorder=2, alpha=1.0, s=3),
     2: dict(color="limegreen",    label="Canopy",         zorder=1, alpha=0.8, s=2),
     3: dict(color="forestgreen",  label="Canopy Top",     zorder=2, alpha=0.9, s=2),
     7: dict(color="red",          label="Built Structure", zorder=2, alpha=0.8, s=2),
    40: dict(color="darkorange",   label="Bathy Floor",    zorder=3, alpha=1.0, s=3),
    41: dict(color="dodgerblue",   label="Water Surface",  zorder=1, alpha=0.6, s=1),
    42: dict(color="dodgerblue",   label="Inland Water",   zorder=1, alpha=0.6, s=1),
}
DEFAULT_STYLE = dict(color="lightgrey", label="Other", zorder=0, alpha=0.3, s=1)

DEM_COLORS = ["dimgrey", "purple", "darkcyan", "darkmagenta", "darkgoldenrod"]



def _granule_id(filepath):
    """Return the bare granule ID from a file path (strips _subsetted and bbox suffixes)."""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    for marker in ("_subsetted", "_W", "_N", "_E", "_S"):
        idx = stem.find(marker)
        if idx >= 0:
            stem = stem[:idx]
    return stem


def _find_h5(nc_path):
    """Search for a matching ATL03 .h5 whose name starts with the same granule ID."""
    gid = _granule_id(nc_path)
    if not gid:
        return None
    search_dirs = [
        os.path.dirname(nc_path),
        os.path.expanduser("~/.ivert/cache/icesat2"),
        os.path.expanduser("~/.ivert/cache"),
    ]
    for d in search_dirs:
        hits = sorted(glob.glob(os.path.join(d, "**", f"{gid}*.h5"), recursive=True))
        if hits:
            return hits[0]
    return None



def _beam_delta_times(h5_path):
    """Return {beam_name: delta_time_array} for all beams present in the .h5."""
    import h5py
    beams = {}
    with h5py.File(h5_path, "r") as f:
        for beam in ["gt1l", "gt1r", "gt2l", "gt2r", "gt3l", "gt3r"]:
            try:
                dt = f[f"{beam}/heights/delta_time"][...]
                beams[beam] = dt
            except KeyError:
                pass
    return beams


def _load_h5_beam_photons(h5_path, beam):
    """Load all photons for one beam from ATL03 .h5, returning a DataFrame with class_code=0.

    Heights are converted from ellipsoidal to EGM2008 geoid by subtracting the
    geoid undulation interpolated from geophys_corr/geoid.  Cumulative along-track
    distance (along_track_m) is computed from geolocation/segment_length and
    heights/dist_ph_along, matching the convention used in the .nc files.
    """
    import h5py
    with h5py.File(h5_path, "r") as f:
        try:
            delta_time    = f[f"{beam}/heights/delta_time"][...]
            lon           = f[f"{beam}/heights/lon_ph"][...]
            lat           = f[f"{beam}/heights/lat_ph"][...]
            h_ph          = f[f"{beam}/heights/h_ph"][...]
            dist_ph_along = f[f"{beam}/heights/dist_ph_along"][...]
            geoid_dt      = f[f"{beam}/geophys_corr/delta_time"][...]
            geoid         = f[f"{beam}/geophys_corr/geoid"][...]
            ph_index_beg  = f[f"{beam}/geolocation/ph_index_beg"][...]
            seg_length    = f[f"{beam}/geolocation/segment_length"][...]
        except KeyError:
            return pd.DataFrame(columns=["x", "y", "z", "delta_time", "class_code", "along_track_m"])

    geoid_ph = np.interp(delta_time, geoid_dt, geoid)
    z = h_ph - geoid_ph

    n = len(delta_time)
    seg_cumul_start = np.concatenate([[0.0], np.cumsum(seg_length[:-1])])
    seg_of_ph = np.clip(
        np.searchsorted(ph_index_beg, np.arange(n), side="right") - 1,
        0, len(ph_index_beg) - 1,
    )
    along_track_m = seg_cumul_start[seg_of_ph] + dist_ph_along

    order = np.argsort(delta_time)
    delta_time    = delta_time[order]
    lon           = lon[order]
    lat           = lat[order]
    z             = z[order]
    along_track_m = along_track_m[order]

    return pd.DataFrame({
        "x":             lon,
        "y":             lat,
        "z":             z,
        "delta_time":    delta_time,
        "class_code":    np.zeros(n, dtype=np.int8),
        "along_track_m": along_track_m,
    })


def load_nc(nc_path):
    """Load the .nc granule into a DataFrame."""
    data = {}
    with netCDF4.Dataset(nc_path) as ds:
        for v in ds.variables:
            raw = ds.variables[v][:]
            arr = raw.data if hasattr(raw, "data") else np.array(raw)
            if np.asarray(arr).dtype.kind == "O":
                arr = np.asarray(arr).astype(str)
            data[v] = arr
    return pd.DataFrame(data)


def _get_vdatum_label(epsg_int):
    """Return a short human-readable label for a vertical datum EPSG code."""
    _KNOWN = {3855: "EGM2008", 5703: "NAVD88", 5714: "MSL", 5773: "EGM96",
              6360: "NAVD88", 5701: "ODN"}
    try:
        import vdatum_lookup
        desc = vdatum_lookup.get_epsg_description(epsg_int)
        if desc:
            return desc.replace(" height", "").replace(" Height", "")
    except Exception:
        pass
    return _KNOWN.get(epsg_int, f"EPSG:{epsg_int}")


def _apply_vdatum_to_df(df, target_vert_epsg_int, cache_dir=None):
    """Return a copy of df with z transformed from EGM2008 to target vertical datum."""
    import transform_points as tp
    src = "EPSG:4326+3855"
    dst = f"EPSG:4326+{target_vert_epsg_int}"
    try:
        _, _, z_new = tp.transform_points(
            df["x"].values, df["y"].values, df["z"].values,
            src_epsg=src, dst_epsg=dst, cache_dir=cache_dir)
        df = df.copy()
        df["z"] = z_new
    except Exception as e:
        print(f"  Warning: vdatum transform failed ({e}). Plotting in EGM2008.", flush=True)
    return df


def _sample_dem_along_track(dem_path, lons, lats, along_track_m,
                             target_vert_epsg_int=None, cache_dir=None):
    """Sample a DEM raster along a laser track at the DEM's native pixel resolution.

    Rather than sampling only at photon locations (which leaves flat gaps between
    clusters), this interpolates the track to an evenly-spaced grid at approximately
    the DEM's pixel size, producing a continuous profile.  Individual DEM grid cells
    may be sampled more than once where the track runs at a shallow angle.

    Returns (along_track_km, z_dem, label) or None if the DEM has no overlap.
    When target_vert_epsg_int is given and differs from the DEM's native vertical datum,
    the sampled elevations are transformed to that datum.
    """
    import rasterio
    import pyproj

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
                    res_m = min(res_crs_x * 111320.0 * np.cos(np.radians(clat)),
                                res_crs_y * 111320.0)
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
                    pyproj.CRS.from_epsg(4326), dem_py_crs, always_xy=True)
                px, py = xformer.transform(dense_lons, dense_lats)
            else:
                px, py = dense_lons.copy(), dense_lats.copy()

            samples = list(src.sample(zip(px.tolist(), py.tolist())))
    except Exception as e:
        print(f"  Warning: could not sample DEM {os.path.basename(dem_path)}: {e}", flush=True)
        return None

    z_dem = np.array([s[0] if len(s) else np.nan for s in samples], dtype=float)

    if dem_nodata is not None:
        z_dem[np.isclose(z_dem, dem_nodata, rtol=0, atol=1e-3)] = np.nan
    valid = np.isfinite(z_dem)
    if not np.any(valid):
        print(f"  DEM {os.path.basename(dem_path)}: no overlap with laser track.", flush=True)
        return None

    if target_vert_epsg_int is not None:
        try:
            import utils.dem_geom as dem_geom
            import transform_points as tp
            _, dem_vert = dem_geom.get_dem_reference_frame_from_file(dem_path)
        except Exception:
            dem_vert = None

        if dem_vert is not None:
            dem_vert_epsg = dem_vert.to_epsg()
            if dem_vert_epsg is not None and dem_vert_epsg != target_vert_epsg_int:
                try:
                    _, _, z_tx = tp.transform_points(
                        dense_lons[valid], dense_lats[valid], z_dem[valid],
                        src_epsg=f"EPSG:4326+{dem_vert_epsg}",
                        dst_epsg=f"EPSG:4326+{target_vert_epsg_int}",
                        cache_dir=cache_dir,
                    )
                    z_out = np.full(len(z_dem), np.nan)
                    z_out[valid] = z_tx
                    z_dem = z_out
                    valid = np.isfinite(z_dem)
                except Exception as e:
                    print(f"  Warning: DEM vertical transform failed: {e}", flush=True)

    sort_idx = np.argsort(dense_atm[valid])
    valid_idx = np.where(valid)[0][sort_idx]
    label = os.path.splitext(os.path.basename(dem_path))[0]
    return dense_atm[valid_idx] / 1000.0, z_dem[valid_idx], label


def _collect_dem_profiles(dem_paths, lons, lats, along_track_m,
                           target_vert_epsg_int, cache_dir):
    """Sample each DEM and return a list of (along_km, z, label) profiles."""
    profiles = []
    for p in (dem_paths or []):
        result = _sample_dem_along_track(p, lons, lats, along_track_m,
                                         target_vert_epsg_int, cache_dir)
        if result is not None:
            print(f"  DEM {os.path.basename(p)}: {len(result[0]):,} sampled points", flush=True)
            profiles.append(result)
    return profiles


def _positions_for_dem_sampling(df, dlim):
    """Return (lons, lats, along_m) restricted to the dlim window (km).

    When dlim is None or both bounds are None, the full arrays are returned.
    This ensures DEM sampling only covers the segment that will actually be plotted,
    so DEMs outside the window are skipped and don't appear in the legend.
    """
    atk_km = df["along_track_m"].values / 1000.0
    lo = dlim[0] if (dlim is not None and dlim[0] is not None) else -np.inf
    hi = dlim[1] if (dlim is not None and dlim[1] is not None) else np.inf
    mask = (atk_km >= lo) & (atk_km <= hi)
    return df["x"].values[mask], df["y"].values[mask], df["along_track_m"].values[mask]


def plot_beam(df_beam, beam_name, outpath, zlim=None, dlim=None, classes=None, title_extra="",
              dem_profiles=None, ylabel=None):
    """Plot one beam's photon curtain (along-track km vs elevation).

    classes: None  → plot all class codes present
             set() → reclassify all classified photons as noise (class 0)
             {1, 40, …} → plot those class codes; all others reclassified as noise
    """
    sort_col = "delta_time" if "delta_time" in df_beam.columns else "y"
    df_beam = df_beam.sort_values(sort_col).reset_index(drop=True)

    # Drop photons with non-physical elevations
    df_beam = df_beam[(df_beam["z"] >= -1e5) & (df_beam["z"] <= 1e5)].reset_index(drop=True)

    # Reclassify photons not in the requested set to noise (class 0) so they still appear
    if classes is not None:
        unselected = (df_beam["class_code"] != 0) & ~df_beam["class_code"].isin(classes)
        df_beam.loc[unselected, "class_code"] = 0

    along_track = df_beam["along_track_m"].values / 1000.0
    z = df_beam["z"].values
    cc = df_beam["class_code"].values

    fig, ax = plt.subplots(figsize=(12, 4))

    for code in np.unique(cc):
        mask = cc == code
        style = CLASS_STYLE.get(int(code), DEFAULT_STYLE)
        ax.scatter(along_track[mask], z[mask],
                   c=style["color"], label=f"{style['label']} (n={mask.sum():,})",
                   zorder=style["zorder"], alpha=style["alpha"], s=style["s"],
                   linewidths=0)

    if dem_profiles:
        for i, (dem_atk, dem_z, dem_lbl) in enumerate(dem_profiles):
            color = DEM_COLORS[i % len(DEM_COLORS)]
            ax.plot(dem_atk, dem_z, color=color, linewidth=0.8, label=dem_lbl,
                    zorder=0.75, alpha=0.75)

    ax.set_xlabel("Along-track distance (km)")
    ax.set_ylabel(ylabel or "Elevation / depth (m, EGM2008 geoid)")
    title = f"{os.path.basename(outpath).replace('.png', '')}  —  {beam_name}"
    if title_extra:
        title += f"  {title_extra}"
    ax.set_title(title, fontsize=8)
    if zlim is not None:
        ax.set_ylim(bottom=zlim[0], top=zlim[1])
    if dlim is not None:
        ax.set_xlim(left=dlim[0], right=dlim[1])
    ax.legend(loc="upper right", fontsize=7, markerscale=2)
    ax.grid(True, linewidth=0.3, alpha=0.5)
    fig.tight_layout()
    fig.savefig(outpath, dpi=200)
    plt.close(fig)
    print(f"  Saved {outpath}")


def main():
    parser = argparse.ArgumentParser(description="Plot classified ICESat-2 photon curtains.")
    parser.add_argument("input_file",
                        help="Path to the .nc granule file, or an ATL03 .h5 file "
                             "(automatically enables --h5-only).")
    parser.add_argument("--laser", "-b", default=None,
                        help="Laser/beam to plot (e.g. gt2l). Default: plot all beams.")
    parser.add_argument("--outdir", "-o", default=None,
                        help="Output directory for images (default: same dir as input file).")
    parser.add_argument("--zmin", type=float, default=None,
                        help="Minimum elevation to display (m). Data outside [-1e5, 1e5] is "
                             "always filtered regardless.")
    parser.add_argument("--zmax", type=float, default=None,
                        help="Maximum elevation to display (m).")
    parser.add_argument("--xmin", type=float, default=None,
                        help="Minimum along-track distance to display (km).")
    parser.add_argument("--xmax", type=float, default=None,
                        help="Maximum along-track distance to display (km).")
    parser.add_argument("--classes", default=None,
                        help="Slash-separated class codes to highlight (e.g. '1/40/41'). "
                             "Photons not in the list are reclassified as noise and shown "
                             "in grey. Default: show all classes. Pass '' to show all "
                             "photons as noise.")
    parser.add_argument("--h5", nargs="?", const=True, default=None,
                        help="ATL03 .h5 file to use as noise background. Supply a path, or "
                             "omit the path to search the IVERT cache for a file whose name "
                             "starts with the same granule ID as the .nc file.")
    parser.add_argument("--h5-only", action="store_true", default=False,
                        help="Plot only the ATL03 .h5 photons (all as noise); ignore the "
                             ".nc classifications entirely. Automatically enabled when the "
                             "input file is an .h5.")
    parser.add_argument("--dem", nargs="+", default=None, metavar="DEM",
                        help="One or more DEM raster files to profile along the laser track. "
                             "Each overlapping DEM is plotted as a line at its sampled elevations.")
    parser.add_argument("--vdatum", "-V", default=None,
                        help="Target vertical datum for photons and DEMs (e.g. 'navd88', "
                             "'egm2008', 'EPSG:5703'). Transforms ICESat-2 photons from "
                             "EGM2008 and DEM elevations to the given datum so both are "
                             "on the same vertical reference. Default: EGM2008 (no transform).")
    args = parser.parse_args()

    # Resolve vertical datum --------------------------------------------------
    target_vert_epsg_int = None
    ylabel = "Elevation / depth (m, EGM2008 geoid)"
    if args.vdatum:
        try:
            import vdatum_lookup
            vdatum_str = vdatum_lookup.resolve_vdatum(args.vdatum)
        except ImportError:
            vdatum_str = args.vdatum if ":" in args.vdatum else f"EPSG:{args.vdatum}"
        if vdatum_str is None:
            sys.exit(f"Unknown vertical datum: {args.vdatum!r}. "
                     "Use an EPSG code or common name (e.g. 'navd88', 'egm2008').")
        try:
            target_vert_epsg_int = int(str(vdatum_str).split(":")[-1])
        except ValueError:
            sys.exit(f"Could not parse vertical EPSG from {vdatum_str!r}.")
        ylabel = f"Elevation / depth (m, {_get_vdatum_label(target_vert_epsg_int)})"

    # Datum-shift grid cache (use ivert cache if available, else cwd)
    try:
        import utils.configfile
        cache_dir = utils.configfile.Config().cache_directory
    except Exception:
        cache_dir = None

    input_path = os.path.abspath(args.input_file)
    if not os.path.exists(input_path):
        sys.exit(f"File not found: {input_path}")

    zlim = None
    if args.zmin is not None or args.zmax is not None:
        zlim = (args.zmin, args.zmax)

    dlim = None
    if args.xmin is not None or args.xmax is not None:
        dlim = (args.xmin, args.xmax)

    if args.classes is None:
        classes = None
    elif args.classes == "":
        classes = set()
    else:
        classes = {int(c) for c in args.classes.split("/")}

    # ------------------------------------------------------------------ h5-only
    h5_only = args.h5_only or input_path.lower().endswith(".h5")

    if h5_only:
        # Resolve the h5 file to use
        if input_path.lower().endswith(".h5"):
            h5_path = input_path
        elif args.h5 is True:
            h5_path = _find_h5(input_path)
            if h5_path is None:
                sys.exit("--h5-only: no matching .h5 found in cache.")
        elif args.h5:
            h5_path = os.path.abspath(args.h5)
            if not os.path.exists(h5_path):
                sys.exit(f".h5 file not found: {h5_path}")
        else:
            h5_path = _find_h5(input_path)
            if h5_path is None:
                sys.exit("--h5-only: no matching .h5 found in cache.")

        outdir = args.outdir or os.path.dirname(h5_path)
        os.makedirs(outdir, exist_ok=True)
        h5_stem = os.path.splitext(os.path.basename(h5_path))[0]

        print(f"H5-only: {os.path.basename(h5_path)}", flush=True)
        beam_dts = _beam_delta_times(h5_path)
        beams_to_plot = [args.laser] if args.laser else list(beam_dts.keys())

        for beam in beams_to_plot:
            if beam not in beam_dts:
                print(f"  Beam {beam} not in .h5, skipping.")
                continue
            df_plot = _load_h5_beam_photons(h5_path, beam)
            if df_plot.empty:
                print(f"  Beam {beam}: no photons, skipping.")
                continue
            print(f"  Beam {beam}: {len(df_plot):,} photons", flush=True)
            if target_vert_epsg_int:
                df_plot = _apply_vdatum_to_df(df_plot, target_vert_epsg_int, cache_dir)
            _dlons, _dlats, _datm = _positions_for_dem_sampling(df_plot, dlim)
            dem_profiles = _collect_dem_profiles(
                args.dem, _dlons, _dlats, _datm, target_vert_epsg_int, cache_dir)
            outpath = os.path.join(outdir, f"{h5_stem}_{beam}.png")
            plot_beam(df_plot, beam, outpath, zlim=zlim, dlim=dlim, classes=classes,
                      dem_profiles=dem_profiles or None, ylabel=ylabel)
        return

    # ------------------------------------------------------------------ nc + optional h5
    nc_path = input_path
    outdir = args.outdir or os.path.dirname(nc_path)
    os.makedirs(outdir, exist_ok=True)

    print(f"Loading {os.path.basename(nc_path)} ...", flush=True)
    df = load_nc(nc_path)
    nc_stem = os.path.splitext(os.path.basename(nc_path))[0]

    # Resolve .h5 path for beam splitting and noise background
    if args.h5 is True:
        h5_path = _find_h5(nc_path)
        if h5_path is None:
            print("Warning: --h5 given but no matching .h5 found in cache.", flush=True)
    elif args.h5:
        h5_path = os.path.abspath(args.h5)
        if not os.path.exists(h5_path):
            sys.exit(f".h5 file not found: {h5_path}")
    else:
        h5_path = None

    if h5_path:
        print(f"Found .h5: {os.path.basename(h5_path)}", flush=True)
        beam_dts = _beam_delta_times(h5_path)
        beams_to_plot = [args.laser] if args.laser else list(beam_dts.keys())

        for beam in beams_to_plot:
            if beam not in beam_dts:
                print(f"  Beam {beam} not in .h5, skipping.")
                continue

            df_bg = _load_h5_beam_photons(h5_path, beam)
            if df_bg.empty:
                print(f"  Beam {beam}: no h5 photons, skipping.")
                continue

            # Filter nc photons to this beam using the laser column when present;
            # fall back to exact (delta_time, x, y) matching for old nc files.
            if "laser" in df.columns:
                df_beam = df[df["laser"] == beam].copy()
            else:
                df_beam = df.merge(
                    df_bg[["delta_time", "x", "y"]],
                    on=["delta_time", "x", "y"], how="inner"
                )

            if df_beam.empty:
                print(f"  Beam {beam}: no photons in .nc, skipping.")
                continue

            # Ensure along_track_m exists on the nc photons; get it from the h5
            # position match if the nc file predates the field being added.
            if "along_track_m" not in df_beam.columns:
                df_beam = df_beam.merge(
                    df_bg[["delta_time", "x", "y", "along_track_m"]],
                    on=["delta_time", "x", "y"], how="left"
                ).dropna(subset=["along_track_m"])

            print(f"  Beam {beam}: {len(df_beam):,} classified + {len(df_bg):,} background photons", flush=True)
            df_plot = pd.concat([df_bg, df_beam], ignore_index=True)
            if target_vert_epsg_int:
                df_plot = _apply_vdatum_to_df(df_plot, target_vert_epsg_int, cache_dir)
            _dlons, _dlats, _datm = _positions_for_dem_sampling(df_plot, dlim)
            dem_profiles = _collect_dem_profiles(
                args.dem, _dlons, _dlats, _datm, target_vert_epsg_int, cache_dir)
            outpath = os.path.join(outdir, f"{nc_stem}_{beam}.png")
            plot_beam(df_plot, beam, outpath, zlim=zlim, dlim=dlim, classes=classes,
                      dem_profiles=dem_profiles or None, ylabel=ylabel)
    else:
        # No .h5 — use laser/along_track_m from the nc file directly if present.
        if "laser" in df.columns:
            beams_in_nc = sorted(df["laser"].unique())
            beams_to_plot_noh5 = [args.laser] if args.laser else beams_in_nc
            for beam in beams_to_plot_noh5:
                df_beam = df[df["laser"] == beam].copy()
                if df_beam.empty:
                    continue
                if "along_track_m" not in df_beam.columns:
                    print(f"  Beam {beam}: nc has no along_track_m, skipping.", flush=True)
                    continue
                print(f"  Beam {beam}: {len(df_beam):,} photons (nc only)", flush=True)
                if target_vert_epsg_int:
                    df_beam = _apply_vdatum_to_df(df_beam, target_vert_epsg_int, cache_dir)
                _dlons, _dlats, _datm = _positions_for_dem_sampling(df_beam, dlim)
                dem_profiles = _collect_dem_profiles(
                    args.dem, _dlons, _dlats, _datm, target_vert_epsg_int, cache_dir)
                outpath = os.path.join(outdir, f"{nc_stem}_{beam}.png")
                plot_beam(df_beam, beam, outpath, zlim=zlim, dlim=dlim, classes=classes,
                          dem_profiles=dem_profiles or None, ylabel=ylabel)
        else:
            print("No .h5 found and nc has no beam/distance info — cannot plot.", flush=True)


if __name__ == "__main__":
    main()
