"""Run manifests for 'ivert validate': record a run's settings, and replay them with -m/--manifest.

A manifest is a small INI file written into the output directory of every validation
that does work. It records the IVERT version and the effective value of every
'ivert validate' option that can change the results, defaults and config fallbacks
included, so the run can be repeated exactly, by the same user or anyone else, on the
same DEMs or new ones.
"""

import configparser
import datetime
import logging
import os
import tempfile
from pathlib import Path

import click
from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "ivert_manifest.ini"

# 'ivert validate' parameters that are never recorded or applied: the inputs and the
# output directory (recorded for reference in [run] only), and options that don't
# change the results.
UNTRACKED_PARAMS = frozenset(
    {"files_or_directory", "outdir", "overwrite", "list_vdatums", "manifest"},
)

_HEADER = """\
# IVERT validation manifest.
# Re-run these settings with:  ivert validate -m {filename} FILES_OR_DIRECTORY ...
# Options given on the command line override the values here. A blank value means
# "not set" (the option's default); options with several values put one per line.
"""

_RUN_COMMENT = """\
# [run] records the command and the files that were validated when this manifest was
# created. It is for reference only: none of it is used when this manifest is passed
# to 'ivert validate -m'. Name the DEMs (and -o/--outdir) on the command line as usual.
"""

_OPTIONS_COMMENT = """\
# [options] holds the settings applied when this manifest is passed to 'ivert validate -m'.
# The keys are the long option names of 'ivert validate', with dashes as underscores.
"""


def manifest_path(output_dir, dem_name=None):
    """Return where a run's manifest is written.

    A single-DEM run gets '<dem>_ivert_manifest.ini', so separate single-DEM runs that
    share an output directory don't overwrite each other's manifests. A collection run
    gets 'ivert_manifest.ini'.
    """
    if dem_name is None:
        return os.path.join(output_dir, MANIFEST_FILENAME)
    base = os.path.splitext(os.path.basename(dem_name))[0]
    return os.path.join(output_dir, f"{base}_{MANIFEST_FILENAME}")


def tracked_params(command):
    """Return {name: click.Parameter} for the parameters of 'command' a manifest records."""
    return {p.name: p for p in command.params if p.name not in UNTRACKED_PARAMS}


def format_value(value):
    """Turn an option value into its manifest text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "\n".join(format_value(v) for v in value)
    return str(value)


def _format_entry(key, text):
    """Format one 'key = value' entry, indenting continuation lines of multi-line values."""
    lines = text.split("\n")
    entry = f"{key} = {lines[0]}".rstrip()
    for line in lines[1:]:
        entry += f"\n    {line}"
    return entry


def write_manifest(path, version, options, run_info):
    """Write a manifest to 'path', replacing any existing file.

    Args:
        path: the manifest file to write.
        version: the IVERT version doing the run.
        options: {option name: effective value} for every tracked option.
        run_info: {key: value} recorded under [run] for reference only.

    """
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        _HEADER.format(filename=os.path.basename(path)),
        "[ivert]",
        _format_entry("version", version),
        _format_entry("created_utc", created),
        "",
        _RUN_COMMENT + "[run]",
        *(_format_entry(k, format_value(v)) for k, v in run_info.items()),
        "",
        _OPTIONS_COMMENT + "[options]",
        *(_format_entry(k, format_value(v)) for k, v in options.items()),
    ]
    text = "\n".join(parts) + "\n"

    # Write to a temporary file and swap it in, so an interrupted write never leaves
    # a half-written manifest behind.
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory,
        prefix=".ivert_manifest_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        Path(tmp_path).replace(path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _new_parser():
    # interpolation=None: values such as file paths may contain '%'.
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep keys exactly as written
    return parser


def read_manifest(path):
    """Read a manifest.

    Returns:
        (version, options): the IVERT version that wrote it, and {option name: raw text}.

    Raises:
        click.ClickException: the file can't be parsed or lacks [ivert] version or [options].

    """
    parser = _new_parser()
    try:
        with open(path) as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as exc:
        msg = f"Could not read manifest '{path}': {exc}"
        raise click.ClickException(msg) from exc

    if not parser.has_option("ivert", "version"):
        msg = f"'{path}' is not an IVERT manifest: it has no 'version' in an [ivert] section."
        raise click.ClickException(msg)
    if not parser.has_section("options"):
        msg = f"'{path}' is not an IVERT manifest: it has no [options] section."
        raise click.ClickException(msg)
    return parser.get("ivert", "version"), dict(parser.items("options"))


def read_manifest_options(path):
    """Return a manifest's [options] as {name: raw text}, or None if it can't be read."""
    try:
        return read_manifest(path)[1]
    except click.ClickException:
        return None


def _version_mismatch_message(manifest_version, current_version):
    """Pick the warning for a manifest whose options don't match this IVERT's."""
    try:
        manifest_release = Version(manifest_version).release
        current_release = Version(current_version).release
    except InvalidVersion:
        manifest_release = current_release = None

    if manifest_release is not None and manifest_release < current_release:
        return (
            f"This manifest belongs to a previous version of IVERT ({manifest_version}) "
            f"that is no longer compatible with the current version ({current_version})."
        )
    if manifest_release is not None and manifest_release > current_release:
        return (
            f"This manifest was run on a newer version of IVERT ({manifest_version}) "
            f"that is not compatible with the current version ({current_version}). "
            "Upgrade IVERT to the latest version ('ivert upgrade')."
        )
    return (
        f"This manifest (IVERT {manifest_version}) is incompatible with the current "
        f"version of IVERT ({current_version})."
    )


def reconcile_options(
    path,
    manifest_version,
    manifest_options,
    tracked_names,
    current_version,
    interactive,
):
    """Check a manifest's options against the current CLI and settle any differences.

    If the manifest's option names match 'tracked_names' exactly, its options are
    returned unchanged. Otherwise a warning names the version situation and lists the
    extra and missing options, and the user is asked whether to go on without the
    extra options (the missing ones take their current defaults).

    Returns:
        The manifest's options, limited to those the current CLI knows.

    Raises:
        click.ClickException: the user declined, or there is no terminal to ask on.

    """
    extra = sorted(set(manifest_options) - set(tracked_names))
    missing = sorted(set(tracked_names) - set(manifest_options))
    if not extra and not missing:
        return dict(manifest_options)

    lines = [f"{path}: {_version_mismatch_message(manifest_version, current_version)}"]
    if extra:
        lines.append(
            "Options in the manifest that the current version of IVERT doesn't recognize: "
            + ", ".join(extra),
        )
    if missing:
        lines.append(
            "Options the current version of IVERT uses that are missing from the manifest: "
            + ", ".join(missing),
        )
    logger.warning("\n".join(lines))

    question = "Ignore the unrecognized manifest options, and use the current defaults for the missing ones?"
    if not interactive:
        msg = (
            "The manifest doesn't match this version of IVERT, and there is no terminal to "
            "ask whether to go ahead. Edit the manifest so its [options] match "
            "'ivert validate', or run interactively."
        )
        raise click.ClickException(msg)
    if not click.confirm(question, default=False):
        msg = "Stopped: the manifest is incompatible with this version of IVERT."
        raise click.ClickException(msg)

    return {k: v for k, v in manifest_options.items() if k not in extra}


def parse_option(ctx, param, raw, path):
    """Convert a manifest value into what 'param' would receive from the command line.

    The parameter's own click type does the conversion, so ranges and choices are
    checked exactly as they are for command-line values.
    """
    if param.multiple:
        value = tuple(line.strip() for line in raw.splitlines() if line.strip())
    elif raw.strip() == "":
        # A blank value means "not set": the option's own default.
        return param.get_default(ctx)
    else:
        value = raw.strip()
    try:
        return param.type_cast_value(ctx, value)
    except click.BadParameter as exc:
        msg = f"Invalid value for '{param.name}' in manifest '{path}': {exc.message}"
        raise click.ClickException(msg) from exc
