#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
ivert.cli
~~~~~~~~~
Command-line interface for the ICESat-2 Validation of Elevations Reporting Tool (IVERT).
"""

import glob
import logging
import os

# Set NUMEXPR_MAX_THREADS before any import loads NumExpr, to suppress the
# "safe limit" warning on machines with many cores.
if "NUMEXPR_MAX_THREADS" not in os.environ:
    try:
        from utils.parallel_funcs import physical_cpu_count as _physical_cpu_count
    except ImportError:
        from ivert_utils.parallel_funcs import physical_cpu_count as _physical_cpu_count
    os.environ["NUMEXPR_MAX_THREADS"] = str(_physical_cpu_count())

import click

try:
    from ivert_utils.version import __version__ as ivert_version
except ImportError:
    try:
        from utils.version import __version__ as ivert_version
    except ImportError:
        ivert_version = "unknown"


@click.group()
@click.version_option(version=ivert_version, prog_name="ivert")
@click.option(
    "--config", "user_config",
    default=None,
    metavar="PATH",
    help=(
        "Path to a user config file, overriding the default "
        "(~/.ivert/user_config.ini) and the IVERT_USER_CONFIG "
        "environment variable."
    ),
)
@click.option(
    "-v", "--verbosity",
    default=None,
    metavar="LEVEL",
    help=(
        "Logging verbosity: debug, info, warning, or error (case-insensitive). "
        "Overrides the 'verbosity' setting in ivert_defaults.ini for this run. "
        "Change the persistent default with 'ivert options verbosity=<level>'."
    ),
)
@click.pass_context
def ivert_cli(ctx, user_config, verbosity):
    """IVERT: ICESat-2 Validation of Elevations Reporting Tool.

    Run 'ivert <command> --help' for detailed help on any command.
    """
    if user_config:
        os.environ["IVERT_USER_CONFIG"] = os.path.abspath(os.path.expanduser(user_config))

    _VERBOSITY_LEVELS = {
        "debug":   (logging.DEBUG,   "%(levelname)s: %(message)s"),
        "info":    (logging.INFO,    "%(message)s"),
        "warning": (logging.WARNING, "%(message)s"),
        "error":   (logging.ERROR,   "%(message)s"),
    }

    if verbosity is None:
        try:
            from utils.configfile import Config
        except ImportError:
            from ivert_utils.configfile import Config
        verbosity = Config().verbosity

    verbosity_key = str(verbosity).strip().lower()
    if verbosity_key not in _VERBOSITY_LEVELS:
        raise click.BadParameter(
            f"'{verbosity}' is not a valid verbosity level. "
            "Choose from: debug, info, warning, error.",
            param_hint="--verbosity",
        )
    level, fmt = _VERBOSITY_LEVELS[verbosity_key]
    logging.basicConfig(level=level, format=fmt)
    logging.getLogger().setLevel(level)


###############################################################
# options
###############################################################

_OPTIONS_EXCLUDED_KEYS = {"ivert_version", "atlas_sdp_epoch", "project_base_directory"}


class _OptionsGroup(click.Group):
    """Click Group that also accepts 'key=value' arguments as config assignments.

    key=value args are captured in parse_args before Click's subcommand routing
    so that Click never attempts to resolve them as subcommand names.
    """

    def parse_args(self, ctx, args):
        if args and not args[0].startswith("-") and "=" in args[0]:
            ctx.args = list(args)
            return []
        return super().parse_args(ctx, args)

    def invoke(self, ctx):
        if ctx.args:
            click.Command.invoke(self, ctx)
            return
        super().invoke(ctx)


@ivert_cli.group("options", cls=_OptionsGroup, invoke_without_command=True)
@click.pass_context
def options(ctx):
    """Configure IVERT settings and local data directories.

    Typically, run once before using IVERT on a new machine, or when
    changing data directory paths or credentials.
    """
    if ctx.args:
        _options_set_values(ctx.args)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _options_set_values(assignments):
    """Write one or more key=value pairs to the user config file."""
    import configparser as _cp
    try:
        from utils.configfile import Config
    except ImportError:
        from ivert_utils.configfile import Config

    config = Config()

    parsed = []
    for assignment in assignments:
        if "=" not in assignment:
            raise click.UsageError(f"Invalid format '{assignment}'. Use option_name=value.")
        key, _, value = assignment.partition("=")
        key = key.strip().lower()
        if key in _OPTIONS_EXCLUDED_KEYS:
            raise click.UsageError(f"'{key}' is a read-only setting and cannot be changed.")
        if key not in config._config["DEFAULT"]:
            raise click.UsageError(
                f"Unknown setting '{key}'. Run 'ivert options list' to see valid settings.")
        parsed.append((key, value))

    user_path = os.path.abspath(os.path.expanduser(str(config.user_configfile)))
    user_config = _cp.ConfigParser()
    if os.path.exists(user_path):
        user_config.read(user_path)

    for key, value in parsed:
        user_config["DEFAULT"][key] = value
        click.echo(f"  {key} = {value}")

    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(user_path, "w") as f:
        user_config.write(f)

    click.echo(f"\nSaved to {user_path}")


@options.command("list")
def options_list():
    """List all configurable settings and their current values."""
    try:
        from utils.configfile import Config
    except ImportError:
        from ivert_utils.configfile import Config

    config = Config()
    keys = [k for k in config._config["DEFAULT"].keys()
            if k not in _OPTIONS_EXCLUDED_KEYS]

    if not keys:
        click.echo("No configurable settings found.")
        return

    col_w = max(len(k) for k in keys)
    header = click.style(f"{'Setting':<{col_w}}", bold=True)
    click.echo(f"\n  {header}  Value")
    click.echo("  " + "-" * (col_w + 2 + 56))

    for key in keys:
        value = str(getattr(config, key, ""))
        is_user = key in config._user_set_keys
        colored_key = click.style(f"{key:<{col_w}}", fg="cyan")
        source = click.style("[user]", fg="yellow") if is_user else click.style("[default]", fg="bright_black")
        click.echo(f"  {colored_key}  {value:<52}  {source}")

    click.echo(
        f"\n  To change a setting:  ivert options option_name=new_value"
        f"\n  Add quotes around values containing spaces or special characters."
    )


@options.command("reset")
@click.option("-y", "--yes", is_flag=True, default=False,
              help="Skip confirmation prompt.")
def options_reset(yes):
    """Reset all settings to IVERT defaults by deleting the user config file."""
    try:
        from utils.configfile import Config
    except ImportError:
        from ivert_utils.configfile import Config

    config = Config()
    user_path = os.path.abspath(os.path.expanduser(str(config.user_configfile)))

    if not os.path.exists(user_path):
        click.echo("No user config file found — settings are already at defaults.")
        return

    if not yes:
        click.confirm(f"Delete {user_path} and reset all settings to defaults?", abort=True)

    os.remove(user_path)
    click.echo(f"Deleted {user_path}. All settings reset to IVERT defaults.")


###############################################################
# database
###############################################################

@ivert_cli.group("database", invoke_without_command=True)
@click.pass_context
def database(ctx):
    """Manage the local IVERT ICESat-2 photon database.

    Subcommands handle downloading new data, updating existing records,
    and editing or inspecting the database.

    Run 'ivert database <subcommand> --help' for details.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@database.command("list")
@click.option(
    "-a", "--all", "show_all",
    is_flag=True,
    default=False,
    help="Show all fields for each granule instead of the default summary columns.",
)
@click.option(
    "-bo", "--boxes",
    is_flag=True,
    default=False,
    help="Print the unique query bounding boxes used to build the database. Overrides --all.",
)
def database_list(show_all, boxes):
    """List granules currently in the IVERT ICESat-2 database."""
    import tabulate as tabulate_mod

    try:
        from ivert import icesat2_database_v2 as is2db_mod
    except ImportError:
        import icesat2_database_v2 as is2db_mod

    db = is2db_mod.IS2Database()
    gdf = db.open_gdf(verbose=False)

    if gdf is None:
        click.echo(f"No IVERT database found at: {db.db_fname}")
        click.echo("Run 'ivert database download <bbox>' to create one.")
        return

    if len(gdf) == 0:
        click.echo("Database exists but contains no granules.")
        return

    if boxes:
        def _fmt_date(d):
            s = str(int(d))
            return f"{s[:4]}.{s[4:6]}.{s[6:]}"

        unique_boxes = sorted(set(tuple(b) for b in gdf["query_bbox"]))
        rows = [b[:4] + (_fmt_date(b[4]), _fmt_date(b[5])) for b in unique_boxes]
        headers = ["Xmin", "Xmax", "Ymin", "Ymax", "Date Start", "Date End"]
        click.echo(tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple"))
        click.echo(f"\n{len(unique_boxes)} unique query box(es)  —  db: {db.db_fname}")
    elif show_all:
        cols = [c for c in gdf.columns if c != "geometry"]
        rows = []
        for _, row in gdf.iterrows():
            rows.append([str(row[c]) if isinstance(row[c], (list, tuple)) else row[c]
                         for c in cols])
        click.echo(tabulate_mod.tabulate(rows, headers=cols, tablefmt="simple"))
        click.echo(f"\n{len(gdf)} granule(s)  —  db: {db.db_fname}")
    else:
        rows = []
        for _, row in gdf.iterrows():
            rows.append([
                row["filename"],
                row["numphotons"],
                row["numphotons_ground"],
                row["numphotons_bathy_floor"],
                row["numphotons_bathy_surface"],
            ])
        headers = ["File", "Total", "Ground", "BathyFloor", "BathySurf"]
        click.echo(tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple", intfmt=","))
        click.echo(f"\n{len(gdf)} granule(s)  —  db: {db.db_fname}")


@database.command("rebuild")
def database_rebuild():
    """Rebuild the database index from existing .nc granule files on disk."""
    try:
        from ivert import icesat2_database_v2 as is2db_mod
    except ImportError:
        import icesat2_database_v2 as is2db_mod

    db = is2db_mod.IS2Database()
    gdf = db.create_new_database(populate=True, overwrite=True)
    click.echo(f"Rebuilt database with {len(gdf)} granule(s).")


@database.command("delete")
@click.option(
    "-a", "--all", "delete_all",
    is_flag=True,
    default=False,
    help="Also delete all .nc granule data files from the granules directory.",
)
@click.option(
    "-y", "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt and delete immediately.",
)
def database_delete(delete_all, yes):
    """Delete the .gpkg and .blosc database index files.

    The downloaded .nc granule files are kept unless --all is specified.
    """
    try:
        from ivert import icesat2_database_v2 as is2db_mod
    except ImportError:
        import icesat2_database_v2 as is2db_mod
    try:
        from utils.sizeof_format import sizeof_fmt
    except ImportError:
        from ivert_utils.sizeof_format import sizeof_fmt

    db = is2db_mod.IS2Database()

    # Collect what will actually be deleted before touching anything.
    index_files = [f for f in (db.db_fname, db.db_fname_compressed) if os.path.exists(f)]

    nc_files = []
    if delete_all and os.path.isdir(db.granules_dir):
        nc_files = sorted(
            os.path.join(db.granules_dir, fn)
            for fn in os.listdir(db.granules_dir)
            if os.path.splitext(fn)[-1].lower() == ".nc"
        )

    all_files = index_files + nc_files

    if not all_files:
        click.echo("Nothing to delete — no database files found.")
        return

    total_bytes = sum(os.path.getsize(f) for f in all_files)
    click.echo(f"\n  {len(all_files)} file(s) totaling {sizeof_fmt(total_bytes)} will be deleted:")
    for fpath in all_files:
        click.echo(f"    {fpath}  ({sizeof_fmt(os.path.getsize(fpath))})")

    if not yes:
        click.confirm("\nDelete these files?", default=False, abort=True)

    for fpath in index_files:
        os.remove(fpath)
        click.echo(f"Deleted {fpath}")

    if nc_files:
        for fpath in nc_files:
            os.remove(fpath)
        click.echo(f"Deleted {len(nc_files)} .nc granule file(s) from {db.granules_dir}")
    elif delete_all:
        click.echo(f"No .nc files found in {db.granules_dir}")


@database.command("size")
def database_size():
    """Report the number of files and disk size for each part of the database."""
    try:
        from ivert import icesat2_database_v2 as is2db_mod
    except ImportError:
        import icesat2_database_v2 as is2db_mod
    try:
        from utils.sizeof_format import sizeof_fmt
    except ImportError:
        from ivert_utils.sizeof_format import sizeof_fmt

    db = is2db_mod.IS2Database()

    rows = []

    # .gpkg index
    if os.path.exists(db.db_fname):
        rows.append(("gpkg index", 1, sizeof_fmt(os.path.getsize(db.db_fname)), db.db_fname))
    else:
        rows.append(("gpkg index", 0, "—", db.db_fname))

    # .blosc index
    if os.path.exists(db.db_fname_compressed):
        rows.append(("blosc index", 1, sizeof_fmt(os.path.getsize(db.db_fname_compressed)), db.db_fname_compressed))
    else:
        rows.append(("blosc index", 0, "—", db.db_fname_compressed))

    # .nc granule files
    nc_files = (
        [os.path.join(db.granules_dir, fn)
         for fn in os.listdir(db.granules_dir)
         if os.path.splitext(fn)[-1].lower() == ".nc"]
        if os.path.isdir(db.granules_dir) else []
    )
    nc_count = len(nc_files)
    nc_bytes = sum(os.path.getsize(f) for f in nc_files) if nc_files else 0
    rows.append((".nc granules", nc_count, sizeof_fmt(nc_bytes) if nc_files else "—", db.granules_dir))

    import tabulate as tabulate_mod
    click.echo(tabulate_mod.tabulate(rows, headers=["Type", "Files", "Size", "Path"], tablefmt="simple"))


@database.command("download")
@click.argument("bbox_or_files", nargs=-1, required=True)
@click.option(
    "-ds", "--date-start", "date_start",
    default="one year and one week ago",
    show_default=True,
    help=(
        "Start date for the ICESat-2 data search. Accepts any format supported "
        "by Python's dateparser library (e.g., '2023.01.01', '1 year ago')."
    ),
)
@click.option(
    "-de", "--date-end", "date_end",
    default="one week ago",
    show_default=True,
    help=(
        "End date for the ICESat-2 data search. Must be after --date-start. "
        "The default one-week buffer accounts for processing delays in ICESat-2 "
        "derived products (ATL08, ATL24, etc.)."
    ),
)
@click.option(
    "-p", "--projection",
    default="EPSG:4326",
    show_default=True,
    help="Horizontal projection (EPSG code) that the bounding box coordinates are in.",
)
@click.option(
    "--wsen",
    is_flag=True,
    default=False,
    help=(
        "Treat BBOX as W/S/E/N order (lower-left, upper-right). "
        "Default order is W/E/S/N (Xmin/Xmax/Ymin/Ymax)."
    ),
)
@click.option(
    "-r", "--replace",
    is_flag=True,
    default=False,
    help=(
        "Replace any previously downloaded data that overlaps the requested "
        "region in space and time. Default: keep existing data and only fill gaps."
    ),
)
@click.option(
    "-c", "--classes",
    default="1/6/7/9/40/41/42",
    show_default=True,
    help=(
        "ICESat-2 photon classes to download, slash-separated. "
        "-1=unclassified, 0=noise, 1=ground, 2=canopy, 3=canopy-top, "
        "6=land-ice, 7=buildings, 9=inland-water, 40=bathy-floor, "
        "41=bathy-surface, 42=lake-surface."
    ),
)
@click.option(
    "-cl", "--confidence-level", "confidence_level",
    type=click.IntRange(1, 4),
    default=1,
    show_default=True,
    help=(
        "Minimum ATL03 signal confidence level to save (1–4). Photons below this "
        "level are discarded before writing to the database. "
        "1=low (keep all), 2=medium, 3=high, 4=very-high."
    ),
)
@click.option(
    "-bc", "--bathy-confidence", "bathy_confidence",
    type=click.FloatRange(0.0, 1.0),
    default=0.01,
    show_default=True,
    help=(
        "Minimum ATL24 bathymetry confidence to save (0.0–1.0). "
        "Bathy-floor photons (class 40) below this confidence are discarded "
        "before writing to the database."
    ),
)
@click.option(
    "-f", "--force",
    is_flag=True,
    default=False,
    help=(
        "Skip the interactive prompt when the requested date range extends beyond "
        "the ATL24 data cutoff date. A warning is still printed."
    ),
)
def database_download(bbox_or_files, date_start, date_end, projection, wsen, replace,
                      classes, confidence_level, bathy_confidence, force):
    """Download ICESat-2 photon data for a region of interest.

    BBOX_OR_FILES: A 4-value bounding box in W/E/S/N order (slash-separated,
    e.g., -74.0/-73.0/40.5/41.0), or one or more DEM files whose combined
    extent defines the download region. Use --wsen to switch to W/S/E/N order.

    Examples:

        ivert database download -- -74.0/-73.0/40.5/41.0

        ivert database download -ds 2023.01.01 -de 2024.01.01 ../dems/oregon_coast_v1.tif

        ivert databasee download -ds "two years ago" -de "one year ago" ../dems/*.tif

    (Note: Use the '--' delimiter to explicitly end your command-line options if coordinates begin with a negative '-')
    """
    try:
        from ivert import icesat2_database_v2 as is2db_mod
        from ivert.utils import dem_geom
    except ImportError:
        import icesat2_database_v2 as is2db_mod
        from utils import dem_geom

    # --- Parse bbox or files ---
    # Flatten slash-separated tokens into a flat list.
    values = []
    for token in bbox_or_files:
        values.extend(token.split("/"))

    wgs84_bbox = None  # (xmin, xmax, ymin, ymax)

    if len(values) == 4:
        try:
            nums = [float(v) for v in values]
            if wsen:
                # W/S/E/N → reorder to xmin, xmax, ymin, ymax
                xmin, ymin, xmax, ymax = nums
            else:
                xmin, xmax, ymin, ymax = nums
            if projection.upper() in ("EPSG:4326", "4326"):
                wgs84_bbox = (xmin, xmax, ymin, ymax)
            else:
                wgs84_bbox = dem_geom.get_wgs84_bounding_box(
                    (xmin, xmax, ymin, ymax), dem_horz_reference_frame=projection)
        except ValueError:
            pass  # not numeric — fall through to file path handling

    if wgs84_bbox is None:
        # Treat tokens as file paths (glob-expand them).
        expanded = []
        for token in bbox_or_files:
            matches = glob.glob(token)
            expanded.extend(matches if matches else [token])

        missing = [f for f in expanded if not os.path.exists(f)]
        if missing:
            raise click.ClickException(
                "Files not found (and input is not a valid 4-value bbox): "
                + ", ".join(missing))

        xmins, xmaxs, ymins, ymaxs = [], [], [], []
        for fn in expanded:
            bb = dem_geom.get_wgs84_bounding_box(fn)
            xmins.append(bb[0]); xmaxs.append(bb[1])
            ymins.append(bb[2]); ymaxs.append(bb[3])
        wgs84_bbox = (min(xmins), max(xmaxs), min(ymins), max(ymaxs))

    # --- Parse dates and classes ---
    db = is2db_mod.IS2Database()

    try:
        tmin = db.convert_date_to_yyyymmdd(date_start)
        tmax = db.convert_date_to_yyyymmdd(date_end)
    except Exception as exc:
        raise click.ClickException(f"Could not parse date: {exc}") from exc

    if tmin >= tmax:
        raise click.ClickException(
            f"--date-start ({tmin}) must be before --date-end ({tmax}).")

    # Check ATL24 date cutoff.
    atl24_cutoff = int(db.config.atl24_date_cutoff)
    if tmax > atl24_cutoff:
        c = str(atl24_cutoff)
        cutoff_str = f"{c[:4]}-{c[4:6]}-{c[6:]}"
        t = str(tmax)
        tmax_str = f"{t[:4]}-{t[4:6]}-{t[6:]}"
        click.echo(
            f"WARNING: As of this version of IVERT, ATL24 bathymetry data is not available "
            f"after {cutoff_str}. Data downloaded after that date will lack bathymetry "
            f"classifications (photon classes 40/41).\n"
            f"Your current request ends at {tmax_str}.\n"
            f"You may update this cutoff date via "
            f"'ivert options atl24_date_cutoff=YYYYMMDD' to suppress these warnings if a newer ATL24 "
            f"version has been released.",
            err=True,
        )
        if not force and not click.confirm("\nContinue with the download anyway?", default=False):
            raise click.Abort()

    class_list = tuple(int(c) for c in classes.split("/"))

    full_bbox = (wgs84_bbox[0], wgs84_bbox[1], wgs84_bbox[2], wgs84_bbox[3], tmin, tmax)

    if not is2db_mod.IS2Database.bbox_valid(full_bbox):
        raise click.ClickException(
            f"Invalid bounding box: xmin < xmax, ymin < ymax required. Got {full_bbox[:4]}.")

    db.download_new_granules(full_bbox, classes_to_keep=class_list,
                             min_confidence_level=confidence_level,
                             min_bathy_confidence=bathy_confidence)


###############################################################
# cache
###############################################################

def _cache_dir():
    """Return the configured cache directory path."""
    try:
        from utils.configfile import Config
    except ImportError:
        from ivert_utils.configfile import Config
    return Config().cache_directory


def _fmt_size(nbytes):
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024


@ivert_cli.group("cache", invoke_without_command=True)
@click.pass_context
def cache(ctx):
    """Manage the IVERT local file cache.

    Run 'ivert cache <subcommand> --help' for details.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cache.command("list")
def cache_list():
    """Show the number of files and total size of the cache."""
    import tabulate as tabulate_mod

    cache_dir = _cache_dir()
    if not os.path.isdir(cache_dir):
        click.echo(f"Cache directory does not exist: {cache_dir}")
        return

    # Collect per-top-level-subdir stats, plus a bucket for loose root files.
    subdir_stats = {}   # name -> [file_count, total_bytes]
    for entry in sorted(os.scandir(cache_dir), key=lambda e: e.name):
        if entry.is_dir(follow_symlinks=False):
            count, size = 0, 0
            for dirpath, _, filenames in os.walk(entry.path):
                for fn in filenames:
                    count += 1
                    size += os.path.getsize(os.path.join(dirpath, fn))
            subdir_stats[entry.name] = [count, size]
        elif entry.is_file(follow_symlinks=False):
            subdir_stats.setdefault("(root)", [0, 0])
            subdir_stats["(root)"][0] += 1
            subdir_stats["(root)"][1] += entry.stat().st_size

    if not subdir_stats:
        click.echo(f"Cache is empty: {cache_dir}")
        return

    total_files = sum(v[0] for v in subdir_stats.values())
    total_bytes = sum(v[1] for v in subdir_stats.values())

    rows = [[name, f"{stats[0]:,}", _fmt_size(stats[1])]
            for name, stats in subdir_stats.items()]
    rows.append(["TOTAL", f"{total_files:,}", _fmt_size(total_bytes)])
    click.echo(tabulate_mod.tabulate(rows, headers=["Subdirectory", "Files", "Size"],
                                     tablefmt="simple"))
    click.echo(f"\nCache directory: {cache_dir}")


@cache.command("delete")
@click.option(
    "-f", "--force",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def cache_delete(force):
    """Delete all files in the IVERT cache directory."""
    import shutil

    cache_dir = _cache_dir()
    if not os.path.isdir(cache_dir):
        click.echo(f"Cache directory does not exist: {cache_dir}")
        return

    if not force:
        click.confirm(f"Delete all contents of {cache_dir}?", abort=True)

    deleted_files, deleted_dirs = 0, 0
    for entry in os.scandir(cache_dir):
        if entry.is_dir(follow_symlinks=False):
            deleted_files += sum(len(files) for _, _, files in os.walk(entry.path))
            shutil.rmtree(entry.path)
            deleted_dirs += 1
        else:
            os.remove(entry.path)
            deleted_files += 1

    click.echo(f"Deleted {deleted_dirs} subdirectorie(s) and {deleted_files} root file(s) "
               f"from {cache_dir}")


###############################################################
# validate
###############################################################

def _run_validate(files_or_directory, vdatum, region_name, include_photons,
                  measure_coverage, band_num, outlier_sd_threshold, buildings,
                  confidence_level, bathy_confidence, outdir=None, ndv=None,
                  export_formats=None):
    """Branch to validate_dem or validate_list_of_dems based on the number of input files."""
    verbose = logging.getLogger().level <= logging.INFO
    try:
        from ivert import validate_dem as vd_module
        from ivert import validate_dem_collection as vdc_module
        from ivert import vdatum_lookup
    except ImportError:
        import validate_dem as vd_module
        import validate_dem_collection as vdc_module
        import vdatum_lookup

    # Resolve common datum names (e.g. 'navd88') to 'EPSG:NNNN' strings.
    if vdatum != "NONE_PROVIDED":
        resolved = vdatum_lookup.resolve_vdatum(vdatum)
        if resolved is None:
            raise click.ClickException(
                f"Unrecognised vertical datum '{vdatum}'. "
                "Provide an EPSG code (e.g. 'EPSG:5703', '5703') or a known short name "
                "(e.g. 'navd88', 'egm2008', 'mllw'). "
                "Run 'ivert validate --list-vdatums' to see all recognised names."
            )
        vdatum = resolved

    # Parse the --ndv value: "nan" → float('nan'), else convert to float.
    ndv_float = None
    if ndv is not None:
        if str(ndv).lower() == "nan":
            ndv_float = float("nan")
        else:
            try:
                ndv_float = float(ndv)
            except ValueError:
                raise click.ClickException(
                    f"Invalid --ndv value '{ndv}'. Provide a number or 'nan'."
                )

    # Resolve the export-formats override. None means "use the config default"; an
    # explicit 'none'/empty value means "skip error exports for this run".
    export_error_formats = None
    if export_formats is not None:
        if str(export_formats).strip().lower() in ("none", ""):
            export_error_formats = []
        else:
            export_error_formats = export_formats

    if outdir is None:
        try:
            from utils.configfile import Config
        except ImportError:
            from ivert_utils.configfile import Config
        # Read the raw (unresolved) string so it stays relative to the DEM directory,
        # not the config file's directory.
        outdir = Config()._config["DEFAULT"]["ivert_results_subdir"]

    # Expand any glob patterns the shell left unexpanded (e.g., quoted patterns).
    expanded = []
    for f in files_or_directory:
        matches = glob.glob(f)
        expanded.extend(matches if matches else [f])

    if not expanded:
        raise click.ClickException("No input files or directory found.")

    classes = [1, 6, 40]
    if buildings:
        classes = sorted(classes + [7])

    if len(expanded) == 1 and os.path.isfile(expanded[0]):
        # validate_dem uses output_dir as-is, so resolve any relative path against
        # the DEM's own directory rather than the current working directory.
        if not os.path.isabs(outdir):
            single_outdir = os.path.join(os.path.dirname(os.path.abspath(expanded[0])), outdir)
        else:
            single_outdir = outdir
        kwargs = dict(
            dem_name=expanded[0],
            output_dir=single_outdir,
            classes=classes,
            band_num=band_num,
            outliers_sd_threshold=outlier_sd_threshold,
            include_photon_level_validation=include_photons,
            location_name=region_name,
            measure_coverage=measure_coverage,
            min_confidence_level=confidence_level,
            min_bathy_confidence=bathy_confidence,
            verbose=verbose,
        )
        if vdatum != "NONE_PROVIDED":
            kwargs["dem_vertical_datum"] = vdatum
        if ndv_float is not None:
            kwargs["dem_ndv"] = ndv_float
        if export_error_formats is not None:
            kwargs["export_error_formats"] = export_error_formats
        vd_module.validate_dem(**kwargs)
    else:
        dem_input = expanded[0] if len(expanded) == 1 else expanded
        if not os.path.isabs(outdir):
            if isinstance(dem_input, list):
                dem_dir = os.path.dirname(os.path.abspath(dem_input[0]))
            elif os.path.isdir(dem_input):
                dem_dir = os.path.abspath(dem_input)
            else:
                dem_dir = os.path.dirname(os.path.abspath(dem_input))
            multi_outdir = os.path.join(dem_dir, outdir)
        else:
            multi_outdir = outdir
        kwargs = dict(
            dem_list_or_dir=dem_input,
            output_dir=multi_outdir,
            classes=classes,
            band_num=band_num,
            place_name=region_name,
            include_photon_validation=include_photons,
            measure_coverage=measure_coverage,
            outliers_sd_threshold=outlier_sd_threshold,
            min_confidence_level=confidence_level,
            min_bathy_confidence=bathy_confidence,
            verbose=verbose,
        )
        if vdatum != "NONE_PROVIDED":
            kwargs["input_vdatum"] = vdatum
        if ndv_float is not None:
            kwargs["dem_ndv"] = ndv_float
        if export_error_formats is not None:
            kwargs["export_error_formats"] = export_error_formats
        vdc_module.validate_list_of_dems(**kwargs)


@ivert_cli.command("validate")
@click.argument("files_or_directory", nargs=-1, required=False)
@click.option(
    "-V", "--vdatum",
    default="NONE_PROVIDED",
    show_default=False,
    help=(
        "Vertical datum of the input DEM(s). Accepts an EPSG code "
        "('EPSG:5703', '5703'), a bare integer, or a common short name "
        "('navd88', 'egm2008', 'mllw', …). If omitted, IVERT reads the datum "
        "from the DEM metadata header. Use --list-vdatums to see all "
        "recognised names."
    ),
)
@click.option(
    "--list-vdatums",
    is_flag=True,
    default=False,
    help="Print all recognised vertical datum names and their EPSG codes, then exit.",
)
@click.option(
    "-n", "--name", "--region-name", "region_name",
    default=None,
    show_default=False,
    help=(
        "Name of the region being validated. For a single DEM, appears on its "
        "plot (defaults to the DEM filename). For a collection, appears only on "
        "the collection-level summary plot; individual DEM plots always use "
        "their filenames."
    ),
)
@click.option(
    "-ph", "--include-photons", "include_photons",
    is_flag=True,
    default=False,
    help=(
        "Return a point database of individual ICESat-2 photons used to validate "
        "each DEM, in addition to the normal .h5 and .tif results outputs."
    ),
)
@click.option(
    "-mc", "--measure-coverage", "measure_coverage",
    is_flag=True,
    default=False,
    help=(
        "Measure relative photon coverage per grid cell (fraction of 15x15 "
        "sub-regions containing photons). Useful for post-processing "
        "coarse-resolution DEMs where sampling bias may matter."
    ),
)
@click.option(
    "-bn", "--band-num", "band_num",
    type=int,
    default=1,
    show_default=True,
    help="Raster band to validate in each DEM (1-indexed). Other bands are ignored.",
)
@click.option(
    "-sd", "--outlier-sd", "outlier_sd_threshold",
    type=float,
    default=2.5,
    show_default=True,
    help=(
        "Standard-deviation threshold for outlier filtering. Errors more than "
        "this many SDs from the mean are treated as noise and removed. "
        "Use -1 to disable outlier filtering."
    ),
)
@click.option(
    "-b", "--buildings",
    is_flag=True,
    default=False,
    help="Include building-class photons in validation.",
)
@click.option(
    "-cl", "--confidence-level", "confidence_level",
    type=click.IntRange(1, 4),
    default=4,
    show_default=True,
    help=(
        "Minimum ATL03 signal confidence level to use (1–4). Photons below this "
        "level are excluded from validation. "
        "1=low (keep all), 2=medium, 3=high, 4=very-high."
    ),
)
@click.option(
    "-bc", "--bathy-confidence", "bathy_confidence",
    type=click.FloatRange(0.0, 1.0),
    default=0.90,
    show_default=True,
    help=(
        "Minimum ATL24 bathymetry confidence to use (0.0–1.0). "
        "Bathy-floor photons (class 40) below this confidence are excluded "
        "from validation."
    ),
)
@click.option(
    "-o", "--outdir",
    default=None,
    metavar="DIR",
    help=(
        "Output directory for validation results. Relative paths are resolved "
        "relative to the input DEM's directory. Defaults to the "
        "'ivert_results_subdir' setting (run 'ivert options list' to view)."
    ),
)
@click.option(
    "--ndv",
    default=None,
    metavar="VALUE",
    help=(
        "No-data value to exclude from DEM pixels before validation. "
        "Accepts a number (e.g. -9999) or 'nan' for IEEE floating-point NaN. "
        "Overrides any no-data value in the DEM file header. "
        "If not set, the file header value is used, falling back to the "
        "config default (dem_default_ndv)."
    ),
)
@click.option(
    "-ef", "--export-formats", "export_formats",
    default=None,
    metavar="FORMATS",
    help=(
        "Comma-separated GIS formats to export the per-cell errors into, drawn from "
        "'tif', 'gpkg', 'shp', 'xyz'. Overrides the 'export_error_formats' setting "
        "for this run only. Pass 'none' (or an empty string) to skip error exports."
    ),
)
def validate(files_or_directory, vdatum, list_vdatums, region_name, include_photons,
             measure_coverage, band_num, outlier_sd_threshold, buildings,
             confidence_level, bathy_confidence, outdir, ndv, export_formats):
    """Validate one or more DEMs against ICESat-2 photon data.

    FILES_OR_DIRECTORY can be one or more GeoTIFF paths, a directory
    (all *.tif files are used), or a glob pattern (e.g., 'data/ncei*.tif').

    Example: ivert validate mydem.tif -V navd88 -n "Oregon Coast"
    """
    if list_vdatums:
        try:
            from ivert import vdatum_lookup
        except ImportError:
            import vdatum_lookup
        name_table, desc_table = vdatum_lookup._get_tables()
        by_epsg: dict = {}
        for name, epsg in name_table.items():
            by_epsg.setdefault(epsg, []).append(name)
        click.echo("Recognised vertical datum names (EPSG code → common names, description):\n")
        for epsg in sorted(by_epsg):
            aliases = sorted(by_epsg[epsg], key=len)
            description = desc_table.get(epsg, "")
            alias_str = ", ".join(f"'{a}'" for a in aliases)
            desc_str = f"  — {description}" if description else ""
            click.echo(f"  EPSG:{epsg:<6d}  {alias_str}{desc_str}")
        return

    if not files_or_directory:
        raise click.UsageError("Missing argument 'FILES_OR_DIRECTORY'.")

    _run_validate(files_or_directory, vdatum, region_name, include_photons,
                  measure_coverage, band_num, outlier_sd_threshold, buildings,
                  confidence_level, bathy_confidence, outdir, ndv=ndv,
                  export_formats=export_formats)


###############################################################
# upgrade
###############################################################

@ivert_cli.command("upgrade")
def upgrade():
    """Upgrade IVERT to the latest available version."""
    try:
        from ivert import client_upgrade
    except ImportError:
        import client_upgrade
    client_upgrade.upgrade()


if __name__ == "__main__":
    ivert_cli()
