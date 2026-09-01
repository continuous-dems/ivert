"""Command-line interface for the ICESat-2 Validation of Elevations Reporting Tool (IVERT)."""

import contextlib
import glob
import logging
import os
import sys
import typing
from pathlib import Path

# Set NUMEXPR_MAX_THREADS before any import loads NumExpr, to suppress the
# "safe limit" warning on machines with many cores.
if "NUMEXPR_MAX_THREADS" not in os.environ:
    from ivert.utils.parallel_funcs import physical_cpu_count as _physical_cpu_count

    os.environ["NUMEXPR_MAX_THREADS"] = str(_physical_cpu_count())

import click

from ivert import __version__ as ivert_version


@click.group()
@click.version_option(version=ivert_version, prog_name="ivert")
@click.option(
    "--config",
    "user_config",
    default=None,
    metavar="PATH",
    help=(
        "Path to a user config file, overriding the default "
        "(~/.ivert/user_config.ini) and the IVERT_USER_CONFIG "
        "environment variable."
    ),
)
@click.option(
    "-v",
    "--verbosity",
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
        os.environ["IVERT_USER_CONFIG"] = os.path.abspath(
            os.path.expanduser(user_config),
        )

    _verbosity_levels = {
        "debug": (logging.DEBUG, "%(levelname)s: %(message)s"),
        "info": (logging.INFO, "%(message)s"),
        "warning": (logging.WARNING, "%(message)s"),
        "error": (logging.ERROR, "%(message)s"),
    }

    if verbosity is None:
        from ivert.utils.configfile import Config

        verbosity = Config().verbosity

    verbosity_key = str(verbosity).strip().lower()
    if verbosity_key not in _verbosity_levels:
        raise click.BadParameter(
            f"'{verbosity}' is not a valid verbosity level. "
            "Choose from: debug, info, warning, error.",
            param_hint="--verbosity",
        )
    level, fmt = _verbosity_levels[verbosity_key]
    logging.basicConfig(level=level, format=fmt)
    logging.getLogger().setLevel(level)


###############################################################
# setup
###############################################################

# NASA Earthdata Login host used for authenticating ICESat-2 data downloads.
_EARTHDATA_MACHINE = "urs.earthdata.nasa.gov"


def _netrc_path():
    """Return the path to the user's .netrc file."""
    return os.path.join(os.path.expanduser("~"), ".netrc")


def _has_earthdata_credentials(netrc_path, machine=_EARTHDATA_MACHINE):
    """Return True if netrc_path has a login and password for the given machine.

    Parses the .netrc token stream directly rather than using the stdlib netrc
    module, which raises on files that are group/world-readable and does not
    tolerate some entries that are otherwise valid for our purposes.
    """
    if not os.path.exists(netrc_path):
        return False

    try:
        with open(netrc_path, encoding="utf-8") as f:
            tokens = f.read().split()
    except OSError:
        return False

    i = 0
    while i < len(tokens):
        if tokens[i] == "machine" and i + 1 < len(tokens) and tokens[i + 1] == machine:
            login = password = None
            j = i + 2
            # Scan this entry until the next machine/default block.
            while j < len(tokens) and tokens[j] not in ("machine", "default"):
                if tokens[j] in ("login", "password", "account") and j + 1 < len(
                    tokens,
                ):
                    if tokens[j] == "login":
                        login = tokens[j + 1]
                    elif tokens[j] == "password":
                        password = tokens[j + 1]
                    j += 2
                else:
                    j += 1
            if login and password:
                return True
            i = j
            continue
        i += 1
    return False


def _append_earthdata_credentials(
    netrc_path,
    username,
    password,
    machine=_EARTHDATA_MACHINE,
):
    """Append a machine entry for the given credentials to netrc_path.

    Existing content is preserved (the file is opened in append mode). The file
    permissions are tightened to owner-only, as .netrc requires.
    """
    prefix = ""
    if os.path.exists(netrc_path):
        with open(netrc_path, encoding="utf-8") as f:
            existing = f.read()
        if existing and not existing.endswith("\n"):
            prefix = "\n"

    entry = f"machine {machine}\n    login {username}\n    password {password}\n"
    with open(netrc_path, "a", encoding="utf-8") as f:
        f.write(prefix + entry)

    # .netrc must be readable only by its owner or tools (and netrc parsers) reject it.
    with contextlib.suppress(OSError):
        Path(netrc_path).chmod(0o600)


def _stdin_is_interactive():
    """Return True if there is a terminal on stdin to prompt the user with."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _check_netrc_permissions(netrc_path):
    """Warn, and offer to fix, if .netrc is readable by anyone but its owner.

    Downloads read credentials through the stdlib netrc module, which refuses
    to parse a group- or world-readable ~/.netrc and reports no credentials at
    all -- so the user would be prompted on every download despite having
    saved them.
    """
    try:
        mode = Path(netrc_path).stat().st_mode
    except OSError:
        return
    if not (mode & 0o077):
        return

    click.echo(
        f"WARNING: {netrc_path} is readable by other users. Credentials in a "
        "file with these permissions are ignored when downloading, so you "
        "would still be asked for them every time.",
        err=True,
    )
    if not _stdin_is_interactive():
        return
    if not click.confirm(
        "Restrict it to owner-only (chmod 600) now?",
        default=True,
    ):
        return
    try:
        Path(netrc_path).chmod(0o600)
        click.echo(f"Set {netrc_path} to owner-only (600).")
    except OSError as e:
        click.echo(f"Could not change the permissions on {netrc_path}: {e}", err=True)


def _setup_earthdata_credentials(announce_if_present=True, prompt_note=None):
    """Check for NASA Earthdata credentials in .netrc, offering to save them.

    Returns True if credentials are in place afterwards. Callers that only want
    to give the user the chance to save them can ignore the result and carry
    on: without a .netrc entry the download prompts for credentials itself,
    once per run. Nothing is prompted when stdin is not a terminal.
    """
    netrc_path = _netrc_path()

    if _has_earthdata_credentials(netrc_path):
        if announce_if_present:
            click.echo(
                "NASA Earthdata credentials are already set in your .netrc file.",
            )
        _check_netrc_permissions(netrc_path)
        return True

    # With no terminal there is no point explaining an offer we cannot make.
    if not _stdin_is_interactive():
        click.echo(
            "No NASA Earthdata credentials are saved in your .netrc file. "
            "Run 'ivert setup' from a terminal to save them.",
        )
        return False

    if os.path.exists(netrc_path):
        click.echo(f"No NASA Earthdata credentials were found in {netrc_path}.")
    else:
        click.echo(
            f"You have no {netrc_path} file, so IVERT has nowhere to save your "
            "NASA Earthdata credentials.",
        )
    click.echo(
        "IVERT needs NASA Earthdata Login credentials to download ICESat-2 data.\n"
        "Register for a free account at https://urs.earthdata.nasa.gov/ if you "
        "do not have one.",
    )
    if prompt_note:
        click.echo(prompt_note)

    if not click.confirm(
        "Would you like to enter and save your NASA Earthdata username and password now?",
        default=False,
    ):
        click.echo(
            "Skipped. Run 'ivert setup' again later to save your credentials.",
        )
        return False

    username = click.prompt("NASA Earthdata username")
    password = click.prompt("NASA Earthdata password", hide_input=True)

    _append_earthdata_credentials(netrc_path, username, password)
    click.echo(f"Saved NASA Earthdata credentials to {netrc_path}.")
    return True


@ivert_cli.command("classes")
def classes():
    """List the ICESat-2 photon classification codes and their meanings.

    These are the class codes assigned to ICESat-2 photons during
    classification and used when filtering photons for validation (e.g. the
    ``--classes`` option of ``ivert database download`` and ``ivert validate``).
    Definitions come from the globato ICESat-2 reader so they always match the
    classifier.
    """
    from ivert.photon_classes import photon_classes

    try:
        classes_list = photon_classes()
    except ImportError as e:
        raise click.ClickException(
            "Could not import globato to read photon class definitions. "
            "Ensure the globato package is installed.",
        ) from e

    if not classes_list:
        click.echo("No photon class definitions found.")
        return

    code_w = max(len("Code"), *(len(str(c)) for c, _ in classes_list))
    header = click.style(f"{'Code':>{code_w}}  Description", bold=True)
    click.echo(f"\n  {header}")
    click.echo("  " + "-" * (code_w + 2 + 40))

    for code, desc in classes_list:
        colored_code = click.style(f"{code:>{code_w}}", fg="cyan", bold=True)
        click.echo(f"  {colored_code}  {desc}")

    click.echo(
        "\n  These codes are used with the --classes option of "
        "'ivert database download', 'ivert database export', and 'ivert validate'.",
    )


@ivert_cli.command("setup")
def setup():
    """Create IVERT's local data directories and check NASA Earthdata credentials.

    Run once on a new machine. Creates the ~/.ivert data directories and verifies
    that NASA Earthdata Login credentials are stored in your ~/.netrc file, offering
    to save them if they are not.
    """
    from ivert.utils.configfile import Config

    config = Config()

    # Derived from the settings IVERT resolved as local paths, so a new path
    # setting is picked up here automatically. A `*_directory` setting names a
    # directory to create; any other path setting names a file, so its parent
    # directory is created instead. Settings that are not local paths -- S3
    # keys, URLs, and ivert_results_subdir, which is resolved against each DEM
    # at validation time -- are not included at all.
    dirs = []
    for key in config.path_options:
        # --config / IVERT_USER_CONFIG can move the config file independently
        # of every other setting, so take the location actually in use.
        path = (
            config.user_config_path
            if key == "user_configfile"
            else getattr(config, key, None)
        )
        if not path:
            continue
        dirs.append(path if key.endswith("_directory") else os.path.dirname(path))

    # Dedupe while preserving order.
    unique_dirs = []
    seen = set()
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            unique_dirs.append(d)

    click.echo("Setting up IVERT data directories...")
    for d in unique_dirs:
        existed = os.path.isdir(d)
        os.makedirs(d, exist_ok=True)
        status = "exists" if existed else "created"
        click.echo(f"  [{status}] {d}")

    click.echo()
    _setup_earthdata_credentials()


###############################################################
# options
###############################################################

# Keys that are read-only and hidden from the "ivert options" command.
_OPTIONS_EXCLUDED_KEYS: set[str] = set()

# Keys that "ivert options" lists but refuses to assign, mapped to the reason
# why, which is shown to the user when they try. Unlike _OPTIONS_EXCLUDED_KEYS
# these still appear in "options list" and "options info" -- their value is
# useful to see, just not settable here.
_OPTIONS_READONLY_KEYS: dict[str, str] = {
    "user_configfile": (
        "IVERT resolves this before it opens your config file, so a config file"
        " cannot move itself. To use a different config file, run"
        " 'ivert --config PATH ...' or set the IVERT_USER_CONFIG environment"
        " variable."
    ),
}


class _OptionsGroup(click.Group):
    """Click Group that also accepts 'key=value' arguments as config assignments.

    key=value args are captured in parse_args before Click's subcommand routing
    so that Click never attempts to resolve them as subcommand names.
    """

    def parse_args(self, ctx, args):
        assignment_positions = {
            i for i, a in enumerate(args) if not a.startswith("-") and "=" in a
        }
        if assignment_positions:
            # Flag this as a bare key=value assignment (not a subcommand) so
            # invoke() routes it to the group callback rather than a subcommand.
            # The group's own flags (e.g. -y) are still parsed by Click; only
            # the assignments are held back, so Click never tries to resolve
            # them as subcommand names.
            ctx.meta["options_passthrough"] = True
            flags = [a for i, a in enumerate(args) if i not in assignment_positions]
            super().parse_args(ctx, flags)
            ctx.args = [args[i] for i in sorted(assignment_positions)]
            return []
        return super().parse_args(ctx, args)

    def invoke(self, ctx):
        # Only a key=value passthrough runs the group callback directly. A real
        # subcommand (e.g. "info <name>") also populates ctx.args with its
        # positional arguments, so those must route through normal dispatch.
        if ctx.meta.get("options_passthrough"):
            click.Command.invoke(self, ctx)
            return
        super().invoke(ctx)


@ivert_cli.group("options", cls=_OptionsGroup, invoke_without_command=True)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help=(
        "Skip the confirmation prompt and also update any settings that are "
        "defined in terms of the ones being changed."
    ),
)
@click.pass_context
def options(ctx, yes):
    """Configure IVERT settings and local data directories.

    Typically, run once before using IVERT on a new machine, or when
    changing data directory paths or credentials.
    """
    if ctx.args:
        _options_set_values(ctx.args, assume_yes=yes)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _option_display_value(config, key):
    """The current value of a setting, as the user should see it."""
    if key == "user_configfile":
        # --config / IVERT_USER_CONFIG can point IVERT at a different file than
        # the one ivert_defaults.ini names, so report the file actually in use.
        return str(config.user_config_path or "")
    return str(getattr(config, key, ""))


def _option_source_label(config, key):
    """Styled marker showing where a setting's current value came from."""
    if key == "user_configfile" and os.environ.get("IVERT_USER_CONFIG"):
        return click.style("[--config]", fg="yellow")
    if key in config._user_set_keys:
        return click.style("[user]", fg="yellow")
    return click.style("[default]", fg="bright_black")


def _inherited_options(config, user_config, changed_keys):
    """Settings that would keep their default value after 'changed_keys' change.

    A setting like "cache_directory = %(user_data_directory)s/cache" lives only
    in ivert_defaults.ini, so it keeps resolving against the *default*
    user_data_directory once the user overrides that setting in their own file.
    Returns (dependents, support): the settings to offer to copy, plus any
    further settings their raw values reference that must be copied alongside
    them so the user config file stays resolvable.

    Settings the user has already set by hand are excluded -- an explicit choice
    is never overwritten, and one that already embeds "%(changed_key)s" picks up
    the new value on its own.
    """
    already_defined = set(user_config["DEFAULT"].keys())

    dependents = [
        k
        for k in config.dependent_options(changed_keys)
        if k not in already_defined
        and k not in _OPTIONS_EXCLUDED_KEYS
        and k not in _OPTIONS_READONLY_KEYS
    ]
    if not dependents:
        return [], []

    support = config.interpolation_support_options(
        dependents,
        already_defined | set(dependents),
    )
    return dependents, support


def _confirm_inherited_copy(config, dependents, changed_keys, assume_yes):
    """Ask whether settings inheriting a changed value should follow it."""
    # The warning exists to flag settings that would silently stay behind. With
    # --yes they are all updated, so there is nothing to warn about.
    if assume_yes:
        return True

    # Name only the changed settings something is actually inheriting from, so
    # unrelated assignments in the same command are not mentioned.
    offered = set(dependents)
    inherited_from = [
        k for k in changed_keys if offered & set(config.dependent_options([k]))
    ]
    changed_list = ", ".join(f"'{k}'" for k in inherited_from or changed_keys)

    click.echo("")
    click.echo(
        f"{click.style('Warning:', fg='yellow')} these settings use the value of"
        f" {changed_list} in their own values, and are still at IVERT's default:",
    )
    for key in dependents:
        click.echo(f"    {click.style(key, fg='cyan')} = {config.raw_default(key)}")

    if not sys.stdin.isatty():
        click.echo(
            "  Leaving them unchanged (no terminal available to ask at)."
            " Re-run with --yes to update them too, or set them individually.",
        )
        return False

    return click.confirm(
        f"  Update them to use the new value of {changed_list}?",
        default=True,
    )


def _options_set_values(assignments, assume_yes=False):
    """Write one or more key=value pairs to the user config file."""
    import configparser as _cp

    from ivert.utils.configfile import Config

    config = Config()

    parsed = []
    for assignment in assignments:
        if "=" not in assignment:
            raise click.UsageError(
                f"Invalid format '{assignment}'. Use option_name=value.",
            )
        key, _, value = assignment.partition("=")
        key = key.strip().lower()
        if key in _OPTIONS_EXCLUDED_KEYS:
            raise click.UsageError(
                f"'{key}' is a read-only setting and cannot be changed.",
            )
        if key in _OPTIONS_READONLY_KEYS:
            raise click.UsageError(
                f"'{key}' is a read-only setting and cannot be changed.\n"
                f"  {_OPTIONS_READONLY_KEYS[key]}",
            )
        if key not in config._config["DEFAULT"]:
            raise click.UsageError(
                f"Unknown setting '{key}'. Run 'ivert options list' to see valid settings.",
            )
        parsed.append((key, value))

    user_path = config.user_config_path
    user_config = _cp.ConfigParser()
    if os.path.exists(user_path):
        user_config.read(user_path)

    for key, value in parsed:
        user_config["DEFAULT"][key] = value
        click.echo(f"  {key} = {value}")

    changed_keys = [k for k, _ in parsed]
    dependents, support = _inherited_options(config, user_config, changed_keys)
    if dependents and _confirm_inherited_copy(
        config,
        dependents,
        changed_keys,
        assume_yes,
    ):
        # Copied raw, so they keep embedding "%(changed_key)s" and will follow
        # the changed setting again if it is ever changed a second time.
        copied = []
        for key in dependents + support:
            try:
                user_config["DEFAULT"][key] = config.raw_default(key)
            except ValueError as e:
                # A hand-edited default with a stray '%' that configparser
                # refuses to store. Skip it rather than losing the whole write.
                click.echo(f"  Skipped '{key}': {e}", err=True)
                continue
            copied.append(key)
        if copied:
            click.echo(f"  Updated: {', '.join(copied)}")

    os.makedirs(os.path.dirname(user_path), exist_ok=True)
    with open(user_path, "w", encoding="utf-8") as f:
        user_config.write(f)

    click.echo(f"\nSaved to {user_path}")


@options.command("list")
@click.option(
    "-d",
    "--details",
    is_flag=True,
    default=False,
    help="Show a description of what each setting means.",
)
def options_list(details):
    """List all configurable settings and their current values."""
    from ivert.utils.configfile import Config, parse_option_descriptions

    config = Config()
    keys = [k for k in config._config["DEFAULT"] if k not in _OPTIONS_EXCLUDED_KEYS]

    if not keys:
        click.echo("No configurable settings found.")
        return

    descriptions = parse_option_descriptions() if details else {}

    if details:
        click.echo("")
        for key in keys:
            value = _option_display_value(config, key)
            source = _option_source_label(config, key)
            click.echo(
                f"  {click.style(key, fg='cyan', bold=True)}  = {value}  {source}",
            )
            desc = descriptions.get(key)
            if desc:
                for line in desc.splitlines():
                    click.echo(f"      {line}")
            else:
                click.echo(
                    click.style("      (no description available)", fg="bright_black"),
                )
            click.echo("")
    else:
        col_w = max(len(k) for k in keys)
        header = click.style(f"{'Setting':<{col_w}}", bold=True)
        click.echo(f"\n  {header}  Value")
        click.echo("  " + "-" * (col_w + 2 + 56))

        for key in keys:
            value = _option_display_value(config, key)
            colored_key = click.style(f"{key:<{col_w}}", fg="cyan")
            source = _option_source_label(config, key)
            click.echo(f"  {colored_key}  {value:<52}  {source}")

    click.echo(
        "\n  To change a setting:  ivert options option_name=new_value"
        "\n  Add quotes around values containing spaces or special characters."
        "\n  For a description of each setting:  ivert options list --details"
        "\n  For one setting:  ivert options info <option_name>",
    )


@options.command("info")
@click.argument("option_name")
def options_info(option_name):
    """Show a description of a single setting, its current value, and default."""
    from ivert.utils.configfile import Config, parse_option_descriptions

    config = Config()
    key = option_name.strip().lower()

    if key in _OPTIONS_EXCLUDED_KEYS or key not in config._config["DEFAULT"]:
        raise click.UsageError(
            f"Unknown setting '{option_name}'. "
            "Run 'ivert options list' to see valid settings.",
        )

    value = _option_display_value(config, key)
    default_value = config._config["DEFAULT"].get(key, "")
    is_user = key in config._user_set_keys
    descriptions = parse_option_descriptions()

    click.echo("")
    click.echo(f"  {click.style(key, fg='cyan', bold=True)}")
    desc = descriptions.get(key)
    if desc:
        for line in desc.splitlines():
            click.echo(f"      {line}")
    else:
        click.echo(
            click.style("      (no description available)", fg="bright_black"),
        )

    click.echo("")
    env_override = key == "user_configfile" and os.environ.get("IVERT_USER_CONFIG")
    if env_override:
        source = "set by --config / IVERT_USER_CONFIG"
    else:
        source = "user-set" if is_user else "default"
    click.echo(f"  Current value: {value}  ({source})")
    if is_user or env_override:
        click.echo(f"  IVERT default: {default_value}")

    if key in _OPTIONS_READONLY_KEYS:
        click.echo(
            f"\n  {click.style('Read-only.', fg='yellow')}"
            f" {_OPTIONS_READONLY_KEYS[key]}",
        )
    else:
        click.echo(f"\n  To change it:  ivert options {key}=<new_value>")


@options.command("reset")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def options_reset(yes):
    """Reset all settings to IVERT defaults by deleting the user config file."""
    from ivert.utils.configfile import Config

    config = Config()
    user_path = config.user_config_path

    if not user_path or not os.path.exists(user_path):
        click.echo("No user config file found — settings are already at defaults.")
        return

    if not yes:
        click.confirm(
            f"Delete {user_path} and reset all settings to defaults?",
            abort=True,
        )

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
    "-a",
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all fields for each granule instead of the default summary columns.",
)
@click.option(
    "-bo",
    "--boxes",
    is_flag=True,
    default=False,
    help="Print the unique query bounding boxes used to build the database. Overrides --all.",
)
def database_list(show_all, boxes):
    """List granules currently in the IVERT ICESat-2 database."""
    import tabulate as tabulate_mod

    from ivert import icesat2_database_v2 as is2db_mod

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

        qcols = list(db._bbox_cols("query_bbox"))
        unique_boxes = sorted(
            {tuple(r) for r in gdf[qcols].itertuples(index=False)},
        )
        rows = [(*b[:4], _fmt_date(b[4]), _fmt_date(b[5])) for b in unique_boxes]
        headers = ["Xmin", "Xmax", "Ymin", "Ymax", "Date Start", "Date End"]
        click.echo(tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple"))
        click.echo(f"\n{len(unique_boxes)} unique query box(es)  —  db: {db.db_fname}")
    elif show_all:
        cols = [c for c in gdf.columns if c != "geometry"]
        rows = []
        for _, row in gdf.iterrows():
            rows.append(
                [
                    str(row[c]) if isinstance(row[c], (list, tuple)) else row[c]
                    for c in cols
                ],
            )
        click.echo(tabulate_mod.tabulate(rows, headers=cols, tablefmt="simple"))
        click.echo(f"\n{len(gdf)} granule(s)  —  db: {db.db_fname}")
    else:
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
        click.echo(
            tabulate_mod.tabulate(rows, headers=headers, tablefmt="simple", intfmt=","),
        )
        click.echo(f"\n{len(gdf)} granule(s)  —  db: {db.db_fname}")


@database.command("rebuild")
def database_rebuild():
    """Rebuild the database index from existing .nc granule files on disk."""
    from ivert import icesat2_database_v2 as is2db_mod

    db = is2db_mod.IS2Database()
    gdf = db.create_new_database(populate=True, overwrite=True)
    click.echo(f"Rebuilt database with {len(gdf)} granule(s).")


@database.command("delete")
@click.option(
    "-a",
    "--all",
    "delete_all",
    is_flag=True,
    default=False,
    help="Also delete all .nc granule data files from the granules directory.",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt and delete immediately.",
)
def database_delete(delete_all, yes):
    """Delete the NetCDF database index file.

    The downloaded .nc granule files are kept unless --all is specified.
    """
    from ivert import icesat2_database_v2 as is2db_mod
    from ivert.utils.sizeof_format import sizeof_fmt

    db = is2db_mod.IS2Database()

    # Collect what will actually be deleted before touching anything.
    index_files = [f for f in (db.db_fname,) if os.path.exists(f)]

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
    click.echo(
        f"\n  {len(all_files)} file(s) totaling {sizeof_fmt(total_bytes)} will be deleted:",
    )
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
        click.echo(
            f"Deleted {len(nc_files)} .nc granule file(s) from {db.granules_dir}",
        )
    elif delete_all:
        click.echo(f"No .nc files found in {db.granules_dir}")


@database.command("size")
def database_size():
    """Report the number of files and disk size for each part of the database."""
    from ivert import icesat2_database_v2 as is2db_mod
    from ivert.utils.sizeof_format import sizeof_fmt

    db = is2db_mod.IS2Database()

    rows = []

    # NetCDF index
    if os.path.exists(db.db_fname):
        rows.append(
            ("index", 1, sizeof_fmt(os.path.getsize(db.db_fname)), db.db_fname),
        )
    else:
        rows.append(("index", 0, "—", db.db_fname))

    # .nc granule files
    nc_files = (
        [
            os.path.join(db.granules_dir, fn)
            for fn in os.listdir(db.granules_dir)
            if os.path.splitext(fn)[-1].lower() == ".nc"
        ]
        if os.path.isdir(db.granules_dir)
        else []
    )
    nc_count = len(nc_files)
    nc_bytes = sum(os.path.getsize(f) for f in nc_files) if nc_files else 0
    rows.append(
        (
            ".nc granules",
            nc_count,
            sizeof_fmt(nc_bytes) if nc_files else "—",
            db.granules_dir,
        ),
    )

    import tabulate as tabulate_mod

    click.echo(
        tabulate_mod.tabulate(
            rows,
            headers=["Type", "Files", "Size", "Path"],
            tablefmt="simple",
        ),
    )


@database.command("download")
@click.argument("bbox_or_files", nargs=-1, required=True)
@click.option(
    "-ds",
    "--date-start",
    "date_start",
    default="one year and one week ago",
    show_default=True,
    help=(
        "Start date for the ICESat-2 data search. Accepts any format supported "
        "by Python's dateparser library (e.g., '2023.01.01', '1 year ago')."
    ),
)
@click.option(
    "-de",
    "--date-end",
    "date_end",
    default="one week ago",
    show_default=True,
    help=(
        "End date for the ICESat-2 data search. Must be after --date-start. "
        "The default one-week buffer accounts for processing delays in ICESat-2 "
        "derived products (ATL08, ATL24, etc.)."
    ),
)
@click.option(
    "-p",
    "--projection",
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
    "-r",
    "--replace",
    is_flag=True,
    default=False,
    help=(
        "Replace any previously downloaded data that overlaps the requested "
        "region in space and time. Default: keep existing data and only fill gaps."
    ),
)
@click.option(
    "-c",
    "--classes",
    default="1/6/7/40/41/42/44",
    show_default=True,
    help=(
        "ICESat-2 photon classes to download, slash-separated. "
        "Run 'ivert classes' for the full list of codes and their meanings."
    ),
)
@click.option(
    "-cl",
    "--confidence-level",
    "confidence_level",
    type=click.IntRange(1, 4),
    default=1,
    show_default=True,
    help=(
        "Minimum ATL03 signal confidence level to save (1-4). Photons below this "
        "level are discarded before writing to the database. "
        "1=low (keep all), 2=medium, 3=high, 4=very-high."
    ),
)
@click.option(
    "-bc",
    "--bathy-confidence",
    "bathy_confidence",
    type=click.FloatRange(0.0, 1.0),
    default=0.01,
    show_default=True,
    help=(
        "Minimum ATL24 bathymetry confidence to save (0.0-1.0). "
        "Bathy-floor photons (class 40) below this confidence are discarded "
        "before writing to the database."
    ),
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Skip the interactive prompt when the requested date range extends beyond "
        "the ATL24 data cutoff date. A warning is still printed."
    ),
)
def database_download(
    bbox_or_files,
    date_start,
    date_end,
    projection,
    wsen,
    replace,
    classes,
    confidence_level,
    bathy_confidence,
    force,
):
    """Download ICESat-2 photon data for a region of interest.

    BBOX_OR_FILES: A 4-value bounding box in W/E/S/N order (slash-separated,
    e.g., -74.0/-73.0/40.5/41.0), or one or more DEM files whose combined
    extent defines the download region. Use --wsen to switch to W/S/E/N order.

    Examples:
        ivert database download -- -74.0/-73.0/40.5/41.0

        ivert database download -ds 2023.01.01 -de 2024.01.01 ../dems/oregon_coast_v1.tif

        ivert database download -ds "two years ago" -de "one year ago" `../dems/*.tif`

    (Note: Use the '--' delimiter to explicitly end your command-line options if coordinates begin with a negative '-')

    """
    from ivert import icesat2_database_v2 as is2db_mod
    from ivert.utils import dem_geom

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
                    (xmin, xmax, ymin, ymax),
                    dem_horz_reference_frame=projection,
                )
        except ValueError:
            pass  # not numeric — fall through to file path handling

    if wgs84_bbox is None:
        # Treat tokens as file paths (glob-expand them).
        expanded = []
        for token in bbox_or_files:
            matches = glob.glob(token)
            expanded.extend(matches or [token])

        missing = [f for f in expanded if not os.path.exists(f)]
        if missing:
            raise click.ClickException(
                "Files not found (and input is not a valid 4-value bbox): "
                + ", ".join(missing),
            )

        xmins, xmaxs, ymins, ymaxs = [], [], [], []
        for fn in expanded:
            bb = dem_geom.get_wgs84_bounding_box(fn)
            xmins.append(bb[0])
            xmaxs.append(bb[1])
            ymins.append(bb[2])
            ymaxs.append(bb[3])
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
            f"--date-start ({tmin}) must be before --date-end ({tmax}).",
        )

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
        if not force and not click.confirm(
            "\nContinue with the download anyway?",
            default=False,
        ):
            raise click.Abort

    class_list = tuple(int(c) for c in classes.split("/"))

    full_bbox = (wgs84_bbox[0], wgs84_bbox[1], wgs84_bbox[2], wgs84_bbox[3], tmin, tmax)

    if not is2db_mod.IS2Database.bbox_valid(full_bbox):
        raise click.ClickException(
            f"Invalid bounding box: xmin < xmax, ymin < ymax required. Got {full_bbox[:4]}.",
        )

    # Offer to save credentials before the download asks for them. Declining is
    # fine -- the download prompts for them itself, just once per run.
    _setup_earthdata_credentials(
        announce_if_present=False,
        prompt_note=(
            "Saving them means you will not be asked again on every download "
            "while you build up your IVERT database."
        ),
    )

    db.download_new_granules(
        full_bbox,
        classes_to_keep=class_list,
        min_confidence_level=confidence_level,
        min_bathy_confidence=bathy_confidence,
        replace=replace,
    )


# Formats offered by 'ivert database export' (a subset of export_vector's
# SUPPORTED_FORMATS; csv is intentionally omitted in favour of the GIS formats).
_EXPORT_FORMATS = ("gpkg", "shp", "xyz")

# Wide YYYYMMDD bounds used to select every granule when no date range is given.
_EXPORT_DATE_MIN = 19000101
_EXPORT_DATE_MAX = 99991231

# Estimated-photon-count threshold above which 'ivert database export' prompts
# for confirmation (unless -f/--force is given).
_EXPORT_WARN_PHOTON_THRESHOLD = 25_000_000

# Vector-file extensions whose extent can define an export region.
_EXPORT_REGION_VECTOR_EXTENSIONS = (
    ".shp",
    ".geojson",
    ".json",
    ".gpkg",
    ".gml",
    ".kml",
)


class _ExportTarget(typing.NamedTuple):
    """What the positional argument of 'ivert database export' resolved to.

    kind is "all" (the whole database), "region" (a bounding box and/or a set of
    polygons), "granule" (one IVERT .nc photon granule file), or "index" (the
    IVERT database index .nc file).
    """

    kind: str
    # WGS84 (xmin, xmax, ymin, ymax), or None.
    bbox: tuple | None
    # A shapely (Multi)Polygon in WGS84 to clip photons to, or None.
    geometry: object | None
    # The input file, for the "granule" and "index" kinds.
    path: str | None


def _region_from_vector_file(path):
    """Return (bbox, geometry) for a polygon-vector file defining an export region.

    The bbox is the file's WGS84 total extent; the geometry is the union of the
    file's polygons (None if the file holds no polygonal geometry, in which case
    the extent alone defines the region).
    """
    import geopandas

    gdf = geopandas.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)

    minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
    bbox = (minx, maxx, miny, maxy)

    polygons = gdf[gdf.geom_type.isin(("Polygon", "MultiPolygon"))]
    geometry = polygons.geometry.union_all() if len(polygons) > 0 else None
    return bbox, geometry


def _region_from_file(path):
    """Return (bbox, geometry) for a raster or polygon-vector file."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _EXPORT_REGION_VECTOR_EXTENSIONS:
        return _region_from_vector_file(path)

    # Otherwise treat it as a raster; the CRS is read from the file header.
    from ivert.utils import dem_geom

    return dem_geom.get_wgs84_bounding_box(path), None


def _resolve_export_target(tokens, projection, wsen):
    """Resolve the positional argument of 'ivert database export' to an _ExportTarget.

    Tokens are one of: nothing (export the entire database), a 4-value
    slash-separated bounding box, or a single path to an IVERT .nc file (a photon
    granule or the database index, auto-detected by content) or to a georeferenced
    raster or polygon-vector file whose area defines the region to export.
    """
    if not tokens:
        return _ExportTarget("all", None, None, None)

    # Flatten slash-separated tokens into a flat list of values.
    values = []
    for token in tokens:
        values.extend(token.split("/"))

    # Try a 4-value numeric bounding box first.
    if len(values) == 4:
        try:
            nums = [float(v) for v in values]
        except ValueError:
            nums = None

        if nums is not None:
            if wsen:
                # W/S/E/N → xmin, ymin, xmax, ymax
                xmin, ymin, xmax, ymax = nums
            else:
                # W/E/S/N → xmin, xmax, ymin, ymax
                xmin, xmax, ymin, ymax = nums

            if projection.upper() in ("EPSG:4326", "4326"):
                bbox = (xmin, xmax, ymin, ymax)
            else:
                from ivert.utils import dem_geom

                bbox = dem_geom.get_wgs84_bounding_box(
                    (xmin, xmax, ymin, ymax),
                    dem_horz_reference_frame=projection,
                )

            return _ExportTarget("region", bbox, None, None)

    # Otherwise the argument must be a single file.
    if len(tokens) != 1:
        raise click.ClickException(
            "Region must be a 4-value bounding box or a single file.",
        )

    path = tokens[0]
    if not os.path.exists(path):
        raise click.ClickException(
            f"'{path}' is neither a valid 4-value bounding box nor an existing file.",
        )

    # An IVERT .nc file is exported directly; which kind it is comes from its contents.
    if os.path.splitext(path)[1].lower() == ".nc":
        from ivert import export_vector as ev

        kind = ev.detect_nc_kind(path)
        if kind == ev.KIND_INDEX:
            return _ExportTarget("index", None, None, path)
        if kind == ev.KIND_PHOTONS:
            return _ExportTarget("granule", None, None, path)
        raise click.ClickException(
            f"'{path}' is not an IVERT photon granule or database index file.",
        )

    try:
        bbox, geometry = _region_from_file(path)
    except Exception as exc:
        raise click.ClickException(
            f"Could not read an export region from '{path}': {exc}",
        ) from exc

    return _ExportTarget("region", bbox, geometry, None)


def _echo_export_summary(written, count, noun):
    """Report the files an export wrote (or that it wrote none)."""
    if not written:
        click.echo(
            "\nNo files written (all target files already exist; use -ow to overwrite).",
        )
        return

    click.echo(f"\nExported {count:,} {noun} to {len(written)} file(s):")
    for path in written:
        click.echo(f"  {path}")


def _export_database_index(index_path, fmt_keys, output, overwrite, filters_given):
    """Export an IVERT database index file as a polygon layer of granule footprints."""
    from ivert import export_vector as ev

    point_formats = [key for key in fmt_keys if key in ("xyz", "csv")]
    if point_formats:
        raise click.ClickException(
            "The database index holds bounding-box polygons rather than points, so it "
            f"cannot be exported as '{', '.join(point_formats)}'. Use 'gpkg' and/or "
            "'shp' instead.",
        )

    if filters_given:
        click.echo(
            "Note: the --classes and date options filter photons, so they do not apply "
            "to a database-index export. Exporting the whole index.",
            err=True,
        )

    gdf = ev.index_to_geodataframe(index_path)
    if len(gdf) == 0:
        raise click.ClickException(f"The database index is empty: {index_path}")

    out_base = output or os.path.join(os.getcwd(), "ivert_database_index")
    written = ev.write_vector_multi(
        gdf,
        out_base,
        fmt_keys,
        overwrite=overwrite,
        kind=ev.KIND_INDEX,
    )
    _echo_export_summary(written, len(gdf), "granule footprints")


def _export_single_granule(
    nc_path,
    fmt_keys,
    output,
    overwrite,
    class_list,
    delta_time_range,
):
    """Export one IVERT .nc photon granule file in its entirety."""
    from ivert import export_vector as ev

    gdf = ev.nc_to_geodataframe(nc_path, classes=class_list)
    if delta_time_range is not None:
        gdf = ev.subset_gdf_to_date_range(gdf, *delta_time_range)

    if len(gdf) == 0:
        raise click.ClickException(
            f"No photons left to export from {os.path.basename(nc_path)} after filtering.",
        )

    out_base = output or os.path.join(
        os.getcwd(),
        os.path.splitext(os.path.basename(nc_path))[0],
    )
    written = ev.write_vector_multi(gdf, out_base, fmt_keys, overwrite=overwrite)
    _echo_export_summary(written, len(gdf), "photons")


@database.command("export")
@click.argument("bbox_or_file", nargs=-1, required=False)
@click.option(
    "-of",
    "--output-format",
    "output_format",
    default="gpkg",
    show_default=True,
    metavar="FORMATS",
    help=(
        "Vector format(s) to export, drawn from 'gpkg', 'shp', 'xyz'. Pass a single "
        "format or a comma-separated combination (e.g. 'gpkg,shp')."
    ),
)
@click.option(
    "-o",
    "--output",
    "output",
    default=None,
    metavar="PATH",
    help=(
        "Output file path. The correct extension is added per format, so multiple "
        "formats share this base name. Default: 'ivert_photons' in the current "
        "directory, or the input file's name when exporting a single .nc file."
    ),
)
@click.option(
    "-c",
    "--classes",
    "classes",
    default=None,
    metavar="CLASSES",
    help=(
        "Slash-separated photon class codes to include (e.g. '1/40/41'). "
        "Default: all classes. Run 'ivert classes' for the full list of codes."
    ),
)
@click.option(
    "-ds",
    "--start-date",
    "--start_date",
    "date_start",
    default=None,
    metavar="DATE",
    help=(
        "Only export photons on or after this date. Accepts any format supported by "
        "Python's dateparser library (e.g. '2023.01.01', '1 year ago'). "
        "Default: no lower date bound."
    ),
)
@click.option(
    "-de",
    "--end-date",
    "--end_date",
    "date_end",
    default=None,
    metavar="DATE",
    help=(
        "Only export photons before this date. Accepts any format supported by "
        "Python's dateparser library. Default: no upper date bound."
    ),
)
@click.option(
    "-p",
    "--projection",
    default="EPSG:4326",
    show_default=True,
    help="Horizontal projection (EPSG code) that the bounding-box coordinates are in.",
)
@click.option(
    "--wsen",
    is_flag=True,
    default=False,
    help=(
        "Treat the bounding box as W/S/E/N order (lower-left, upper-right). "
        "Default order is W/E/S/N (Xmin/Xmax/Ymin/Ymax)."
    ),
)
@click.option(
    "-ow",
    "--overwrite",
    is_flag=True,
    default=False,
    help="Overwrite existing output files. Default: skip formats whose file already exists.",
)
@click.option(
    "-f",
    "--force",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt when the export is estimated to be large.",
)
def database_export(
    bbox_or_file,
    output_format,
    output,
    classes,
    date_start,
    date_end,
    projection,
    wsen,
    overwrite,
    force,
):
    """Export IVERT ICESat-2 photons to GIS vector formats.

    BBOX_OR_FILE (optional) says what to export. It is one of:

    \b
      * nothing — export every photon in the database
      * a 4-value bounding box in W/E/S/N order (slash-separated,
        e.g. -74/-73/40.5/41)
      * a georeferenced raster file, whose extent defines the region
      * a polygon-vector file, whose polygon(s) define the area(s) to export
      * a single IVERT .nc photon granule, exported in its entirety
      * the IVERT database index .nc file, exported as a polygon layer of
        granule footprints (one rectangle per granule, from its data_bbox)

    The last two are auto-detected from the contents of the .nc file.

    Photon outputs carry the same per-photon fields as 'ivert database' stores
    (x, y, z, class_code, class_name, confidence, delta_time, granule_id, and
    bathy_confidence where present). The database index exports every field it
    holds per granule, and cannot be written as 'xyz' since it holds polygons
    rather than points.

    Examples:
        ivert database export

        ivert database export -of gpkg,shp -o bahamas_photons

        ivert database export -- -74/-73/40.5/41 -c 40/41 -ds 2023.01.01 -de 2024.01.01

        ivert database export coastline.gpkg -of xyz

        ivert database export granules/ATL24_20230101_x-74y40.nc

        ivert database export granules/_ivert_database_index.nc -of gpkg,shp

    (Note: Use the '--' delimiter to end command-line options if coordinates begin
    with a negative '-')

    """  # noqa: D301  (\b is click's no-rewrap marker; r""" would break it)
    from ivert import export_vector as ev
    from ivert import icesat2_database_v2 as is2db_mod
    from ivert.icesat2_database_v2 import _yyyymmdd_to_delta_time

    verbose = logging.getLogger().level <= logging.INFO

    # --- Parse output formats. ---
    try:
        fmt_keys = ev.normalize_format_keys(output_format, allowed=_EXPORT_FORMATS)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    # --- Parse class filter. ---
    class_list = None
    if classes:
        try:
            class_list = [
                int(c) for c in classes.replace(",", "/").split("/") if c != ""
            ]
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid --classes value '{classes}': {exc}",
            ) from exc

    # --- Work out what was asked for. ---
    target = _resolve_export_target(list(bbox_or_file), projection, wsen)
    bbox = target.bbox

    db = is2db_mod.IS2Database()

    # --- Parse the optional date range. ---
    date_filtering = date_start is not None or date_end is not None
    tmin, tmax = _EXPORT_DATE_MIN, _EXPORT_DATE_MAX
    if date_filtering:
        try:
            if date_start is not None:
                tmin = db.convert_date_to_yyyymmdd(date_start)
            if date_end is not None:
                tmax = db.convert_date_to_yyyymmdd(date_end)
        except Exception as exc:
            raise click.ClickException(f"Could not parse date: {exc}") from exc
        if tmin >= tmax:
            raise click.ClickException(
                f"--start-date ({tmin}) must be before --end-date ({tmax}).",
            )

    # --- A single .nc file is read straight off disk, with no database lookup. ---
    if target.kind == "index":
        _export_database_index(
            target.path,
            fmt_keys,
            output,
            overwrite,
            filters_given=bool(classes) or date_filtering,
        )
        return

    if target.kind == "granule":
        _export_single_granule(
            target.path,
            fmt_keys,
            output,
            overwrite,
            class_list,
            (
                (_yyyymmdd_to_delta_time(tmin), _yyyymmdd_to_delta_time(tmax))
                if date_filtering
                else None
            ),
        )
        return

    # --- Open the database index. ---
    gdf_index = db.open_gdf(verbose=False)
    if gdf_index is None or len(gdf_index) == 0:
        raise click.ClickException(
            f"No IVERT database found (or it is empty) at: {db.db_fname}\n"
            "Run 'ivert database download <bbox>' to create one.",
        )

    # --- Select the granules to read. ---
    if bbox is None and not date_filtering:
        granule_rows = gdf_index
    else:
        spatial = bbox if bbox is not None else (-180.0, 180.0, -90.0, 90.0)
        bbox6 = (spatial[0], spatial[1], spatial[2], spatial[3], tmin, tmax)
        granule_rows = db.query_granules(bbox6)
        if granule_rows is None:
            granule_rows = gdf_index.iloc[0:0]

    if len(granule_rows) == 0:
        raise click.ClickException(
            "No granules found matching the requested region and/or date range.",
        )

    # --- Warn on large exports. ---
    est_photons = (
        int(granule_rows["numphotons"].sum())
        if "numphotons" in granule_rows.columns
        else 0
    )
    if est_photons >= _EXPORT_WARN_PHOTON_THRESHOLD and not force:
        scope = "the entire database" if bbox is None else "the requested region"
        click.echo(
            f"WARNING: Exporting {scope} covers {len(granule_rows):,} granule(s) with up "
            f"to ~{est_photons:,} photons. This may produce very large output file(s) and "
            f"take a while.",
            err=True,
        )
        if not click.confirm("\nContinue with the export anyway?", default=False):
            raise click.Abort

    # --- Read, subset, and merge granules. ---
    import geopandas
    import pandas as pd

    dt_min = _yyyymmdd_to_delta_time(tmin) if date_filtering else None
    dt_max = _yyyymmdd_to_delta_time(tmax) if date_filtering else None

    total = len(granule_rows)
    click.echo(f"Reading {total:,} granule(s) ...")

    gdfs = []
    for i, (_, row) in enumerate(granule_rows.iterrows(), start=1):
        fpath = os.path.join(db.granules_dir, row["filename"])
        if not os.path.exists(fpath):
            click.echo(f"  Skipping missing granule file: {row['filename']}", err=True)
            continue

        gdf = ev.nc_to_geodataframe(fpath, classes=class_list)
        if bbox is not None:
            gdf = ev.subset_gdf_to_bbox(gdf, bbox)
        if target.geometry is not None:
            gdf = ev.subset_gdf_to_geometry(gdf, target.geometry)
        if date_filtering:
            gdf = ev.subset_gdf_to_date_range(gdf, dt_min, dt_max)

        if len(gdf) > 0:
            gdfs.append(gdf)

        if verbose:
            click.echo(f"  [{i}/{total}] {row['filename']}: {len(gdf):,} photons")

    if not gdfs:
        raise click.ClickException("No photons found to export after filtering.")

    merged = geopandas.GeoDataFrame(
        pd.concat(gdfs, ignore_index=True),
        crs=ev.WGS84_EPSG,
    )

    # --- Write the requested format(s). ---
    out_base = output or os.path.join(os.getcwd(), "ivert_photons")
    written = ev.write_vector_multi(merged, out_base, fmt_keys, overwrite=overwrite)
    _echo_export_summary(written, len(merged), "photons")


###############################################################
# cache
###############################################################


def _cache_dir():
    """Return the configured cache directory path."""
    from ivert.utils.configfile import Config

    return Config().cache_directory


def _fmt_size(nbytes):
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


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
    subdir_stats = {}  # name -> [file_count, total_bytes]
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

    rows = [
        [name, f"{stats[0]:,}", _fmt_size(stats[1])]
        for name, stats in subdir_stats.items()
    ]
    rows.append(["TOTAL", f"{total_files:,}", _fmt_size(total_bytes)])
    click.echo(
        tabulate_mod.tabulate(
            rows,
            headers=["Subdirectory", "Files", "Size"],
            tablefmt="simple",
        ),
    )
    click.echo(f"\nCache directory: {cache_dir}")


@cache.command("delete")
@click.option(
    "-f",
    "--force",
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

    click.echo(
        f"Deleted {deleted_dirs} subdirectorie(s) and {deleted_files} root file(s) "
        f"from {cache_dir}",
    )


###############################################################
# validate
###############################################################

_EXCLUDE_VECTOR_EXTENSIONS = (".shp", ".geojson", ".gpkg")


def _parse_exclude_spec(value, wsen=False):
    """Parse a single -ex/--exclude value into a (minx, miny, maxx, maxy) tuple or a file path.

    Accepts either a 4-value slash-separated bounding box or a path to a
    .shp/.geojson/.gpkg vector file of polygons. Bounding-box coordinates are in the
    DEM's own horizontal CRS. By default the four values are given in W/E/S/N order
    (minx/maxx/miny/maxy); pass wsen=True to interpret them in W/S/E/N order
    (minx/miny/maxx/maxy). Either way, a (minx, miny, maxx, maxy) tuple is returned.
    """
    parts = value.split("/")
    if len(parts) == 4:
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            pass
        else:
            if wsen:
                # W/S/E/N → minx, miny, maxx, maxy
                minx, miny, maxx, maxy = nums
            else:
                # W/E/S/N → minx, maxx, miny, maxy
                minx, maxx, miny, maxy = nums
            return (minx, miny, maxx, maxy)

    if not os.path.exists(value):
        raise click.ClickException(
            f"Invalid --exclude value '{value}': not a 4-value bounding box "
            "and not an existing file path.",
        )
    ext = os.path.splitext(value)[1].lower()
    if ext not in _EXCLUDE_VECTOR_EXTENSIONS:
        raise click.ClickException(
            f"Invalid --exclude file '{value}': expected one of "
            f"{', '.join(_EXCLUDE_VECTOR_EXTENSIONS)}.",
        )
    return value


def _run_validate(
    files_or_directory,
    vdatum,
    region_name,
    include_photons,
    measure_coverage,
    band_num,
    outlier_sd_threshold,
    classes,
    min_photons,
    buildings,
    confidence_level,
    bathy_confidence,
    outdir=None,
    ndv=None,
    export_formats=None,
    overwrite=False,
    exclude_zones=None,
    minimum_coverage_pct=None,
):
    """Branch to validate_dem or validate_list_of_dems based on the number of input files."""
    verbose = logging.getLogger().level <= logging.INFO
    from ivert import validate_dem as vd_module
    from ivert import validate_dem_collection as vdc_module
    from ivert import vdatum_lookup

    # Resolve common datum names (e.g. 'navd88') to 'EPSG:NNNN' strings.
    if vdatum != "NONE_PROVIDED":
        resolved = vdatum_lookup.resolve_vdatum(vdatum)
        if resolved is None:
            raise click.ClickException(
                f"Unrecognised vertical datum '{vdatum}'. "
                "Provide an EPSG code (e.g. 'EPSG:5703', '5703') or a known short name "
                "(e.g. 'navd88', 'egm2008', 'mllw'). "
                "Run 'ivert validate --list-vdatums' to see all recognised names.",
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
                    f"Invalid --ndv value '{ndv}'. Provide a number or 'nan'.",
                ) from None

    # Resolve the export-formats override. None means "use the config default"; an
    # explicit 'none'/empty value means "skip error exports for this run".
    export_error_formats = None
    if export_formats is not None:
        if str(export_formats).strip().lower() in ("none", ""):
            export_error_formats = []
        else:
            export_error_formats = export_formats

    if outdir is None:
        from ivert.utils.configfile import Config

        # Read the raw (unresolved) string so it stays relative to the DEM directory,
        # not the config file's directory.
        outdir = Config()._config["DEFAULT"]["ivert_results_subdir"]

    # Expand any glob patterns the shell left unexpanded (e.g., quoted patterns).
    expanded = []
    for f in files_or_directory:
        matches = glob.glob(f)
        expanded.extend(matches or [f])

    if not expanded:
        raise click.ClickException("No input files or directory found.")

    try:
        class_list = [
            int(c) for c in str(classes).replace(",", "/").split("/") if c != ""
        ]
    except ValueError as exc:
        raise click.ClickException(
            f"Invalid --classes value '{classes}': {exc}",
        ) from exc
    if not class_list:
        raise click.ClickException(
            "--classes must name at least one photon class code. "
            "Run 'ivert classes' for the full list of codes.",
        )
    if buildings and 7 not in class_list:
        class_list.append(7)
    class_list = sorted(set(class_list))

    # Fail early, and legibly, if there is no photon database to validate against.
    # If the granules are on disk but the index file isn't, this rebuilds it.
    from ivert import icesat2_database_v2 as is2db_mod

    try:
        is2db_mod.IS2Database().ensure_index_exists()
    except is2db_mod.DatabaseNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if len(expanded) == 1 and os.path.isfile(expanded[0]):
        # validate_dem uses output_dir as-is, so resolve any relative path against
        # the DEM's own directory rather than the current working directory.
        if not os.path.isabs(outdir):
            single_outdir = os.path.join(
                os.path.dirname(os.path.abspath(expanded[0])),
                outdir,
            )
        else:
            single_outdir = outdir
        kwargs = {
            "dem_name": expanded[0],
            "output_dir": single_outdir,
            "classes": class_list,
            "band_num": band_num,
            "outliers_sd_threshold": outlier_sd_threshold,
            "include_photon_level_validation": include_photons,
            "location_name": region_name,
            "measure_coverage": measure_coverage,
            "min_coverage_pct": minimum_coverage_pct,
            "min_photons_per_cell": min_photons,
            "min_confidence_level": confidence_level,
            "min_bathy_confidence": bathy_confidence,
            "verbose": verbose,
            "overwrite": overwrite,
        }
        if vdatum != "NONE_PROVIDED":
            kwargs["dem_vertical_datum"] = vdatum
        if ndv_float is not None:
            kwargs["dem_ndv"] = ndv_float
        if export_error_formats is not None:
            kwargs["export_error_formats"] = export_error_formats
        if exclude_zones:
            kwargs["exclude_zones"] = exclude_zones
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
        kwargs = {
            "dem_list_or_dir": dem_input,
            "output_dir": multi_outdir,
            "classes": class_list,
            "band_num": band_num,
            "place_name": region_name,
            "include_photon_validation": include_photons,
            "measure_coverage": measure_coverage,
            "min_coverage_pct": minimum_coverage_pct,
            "min_photons_per_cell": min_photons,
            "outliers_sd_threshold": outlier_sd_threshold,
            "min_confidence_level": confidence_level,
            "min_bathy_confidence": bathy_confidence,
            "verbose": verbose,
            "overwrite": overwrite,
        }
        if vdatum != "NONE_PROVIDED":
            kwargs["input_vdatum"] = vdatum
        if ndv_float is not None:
            kwargs["dem_ndv"] = ndv_float
        if export_error_formats is not None:
            kwargs["export_error_formats"] = export_error_formats
        if exclude_zones:
            kwargs["exclude_zones"] = exclude_zones
        vdc_module.validate_list_of_dems(**kwargs)


@ivert_cli.command("validate")
@click.argument("files_or_directory", nargs=-1, required=False)
@click.option(
    "-V",
    "--vdatum",
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
    "-n",
    "--name",
    "--region-name",
    "region_name",
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
    "-ph",
    "--include-photons",
    "include_photons",
    is_flag=True,
    default=False,
    help=(
        "Return a point database of individual ICESat-2 photons used to validate "
        "each DEM, in addition to the normal .h5 and .tif results outputs."
    ),
)
@click.option(
    "-mc",
    "--measure-coverage",
    "measure_coverage",
    is_flag=True,
    default=False,
    help=(
        "Measure relative photon coverage per grid cell (fraction of 15x15 "
        "sub-regions containing photons). Useful for post-processing "
        "coarse-resolution DEMs where sampling bias may matter."
    ),
)
@click.option(
    "-mcp",
    "--minimum-coverage-pct",
    "minimum_coverage_pct",
    type=click.FloatRange(0, 100),
    default=None,
    help=(
        "Only validate grid cells whose measured coverage is at or above this "
        "percentage (0-100); lower-coverage cells are dropped from the results, "
        "statistics, and plots. Requires the -mc/--measure-coverage flag."
    ),
)
@click.option(
    "-bn",
    "--band-num",
    "band_num",
    type=int,
    default=1,
    show_default=True,
    help="Raster band to validate in each DEM (1-indexed). Other bands are ignored.",
)
@click.option(
    "-sd",
    "--outlier-sd",
    "outlier_sd_threshold",
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
    "-c",
    "--classes",
    "classes",
    default="1/6/40",
    show_default=True,
    metavar="CLASSES",
    help=(
        "ICESat-2 photon classes to validate against, slash-separated (e.g. "
        "'1/40'). Photons in any other class are excluded before the elevation "
        "statistics are computed. Run 'ivert classes' for the full list of codes."
    ),
)
@click.option(
    "-mp",
    "--min-photons",
    "min_photons",
    type=click.IntRange(1, None),
    default=3,
    show_default=True,
    help=(
        "Minimum number of photons a grid cell must contain to be validated. "
        "Cells with fewer photons are omitted from the results entirely. Cells "
        "with 5 or more photons have their outliers trimmed to the interdecile "
        "range; cells below that use every photon they contain."
    ),
)
@click.option(
    "-b",
    "--buildings",
    is_flag=True,
    default=False,
    help="Include building-class photons (class 7) in validation, on top of -c/--classes.",
)
@click.option(
    "-cl",
    "--confidence-level",
    "confidence_level",
    type=click.IntRange(1, 4),
    default=4,
    show_default=True,
    help=(
        "Minimum ATL03 signal confidence level to use (1-4). Photons below this "
        "level are excluded from validation. "
        "1=low (keep all), 2=medium, 3=high, 4=very-high."
    ),
)
@click.option(
    "-bc",
    "--bathy-confidence",
    "bathy_confidence",
    type=click.FloatRange(0.0, 1.0),
    default=0.90,
    show_default=True,
    help=(
        "Minimum ATL24 bathymetry confidence to use (0.0-1.0). "
        "Bathy-floor photons (class 40) below this confidence are excluded "
        "from validation."
    ),
)
@click.option(
    "-o",
    "--outdir",
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
    "-ef",
    "--export-formats",
    "export_formats",
    default=None,
    metavar="FORMATS",
    help=(
        "Comma-separated GIS formats to export the per-cell errors into, drawn from "
        "'tif', 'gpkg', 'shp', 'xyz'. Overrides the 'export_error_formats' setting "
        "for this run only. Pass 'none' (or an empty string) to skip error exports."
    ),
)
@click.option(
    "-ow",
    "--overwrite",
    is_flag=True,
    default=False,
    help=(
        "Redo the validation and overwrite existing output files, even if the "
        "validation has already completed (or partially completed) for this DEM. "
        "Default: reuse existing interim/output files and skip work that's already done."
    ),
)
@click.option(
    "-ex",
    "--exclude",
    "exclude",
    multiple=True,
    metavar="BBOX_OR_FILE",
    help=(
        "Exclude ICESat-2 photons falling within a zone before validation. Repeatable "
        "(use multiple times to combine zones). Each use takes either a 4-value "
        "slash-separated bounding box in the DEM's own projection (W/E/S/N order, "
        "minx/maxx/miny/maxy; use --wsen for W/S/E/N order), or a path to a vector "
        "file (.shp, .geojson, .gpkg) containing exclusion polygon(s)."
    ),
)
@click.option(
    "--wsen",
    is_flag=True,
    default=False,
    help=(
        "Treat -ex/--exclude bounding boxes as W/S/E/N order (minx/miny/maxx/maxy). "
        "Default order is W/E/S/N (minx/maxx/miny/maxy)."
    ),
)
def validate(
    files_or_directory,
    vdatum,
    list_vdatums,
    region_name,
    include_photons,
    measure_coverage,
    minimum_coverage_pct,
    band_num,
    outlier_sd_threshold,
    classes,
    min_photons,
    buildings,
    confidence_level,
    bathy_confidence,
    outdir,
    ndv,
    export_formats,
    overwrite,
    exclude,
    wsen,
):
    """Validate one or more DEMs against ICESat-2 photon data.

    FILES_OR_DIRECTORY can be one or more GeoTIFF paths, a directory
    (all `*.tif` files are used), or a glob pattern (e.g., `data/ncei*.tif`).

    Example: ivert validate mydem.tif -V navd88 -n "Oregon Coast"
    """
    if list_vdatums:
        from ivert import vdatum_lookup

        name_table, desc_table = vdatum_lookup._get_tables()
        by_epsg: dict = {}
        for name, epsg in name_table.items():
            by_epsg.setdefault(epsg, []).append(name)
        click.echo(
            "Recognised vertical datum names (EPSG code → common names, description):\n",
        )
        for epsg in sorted(by_epsg):
            aliases = sorted(by_epsg[epsg], key=len)
            description = desc_table.get(epsg, "")
            alias_str = ", ".join(f"'{a}'" for a in aliases)
            desc_str = f"  — {description}" if description else ""
            click.echo(f"  EPSG:{epsg:<6d}  {alias_str}{desc_str}")
        return

    if not files_or_directory:
        raise click.UsageError("Missing argument 'FILES_OR_DIRECTORY'.")

    if minimum_coverage_pct is not None and not measure_coverage:
        raise click.UsageError(
            "--minimum-coverage-pct requires the -mc/--measure-coverage flag "
            "(coverage must be measured before it can be filtered on).",
        )

    exclude_zones = (
        [_parse_exclude_spec(value, wsen=wsen) for value in exclude]
        if exclude
        else None
    )

    _run_validate(
        files_or_directory,
        vdatum,
        region_name,
        include_photons,
        measure_coverage,
        band_num,
        outlier_sd_threshold,
        classes,
        min_photons,
        buildings,
        confidence_level,
        bathy_confidence,
        outdir,
        ndv=ndv,
        export_formats=export_formats,
        overwrite=overwrite,
        exclude_zones=exclude_zones,
        minimum_coverage_pct=minimum_coverage_pct,
    )


if __name__ == "__main__":
    ivert_cli()
