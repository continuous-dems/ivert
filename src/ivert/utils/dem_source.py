"""Work out which raster a DEM path refers to: a plain file, or one variable inside a NetCDF file.

A NetCDF file with a single data variable opens as an ordinary raster. One with several
variables opens with no bands of its own, so the elevation variable has to be picked out
as a GDAL subdataset string such as 'NETCDF:"/data/dem.nc":elev'. The rest of IVERT passes
that string around in place of a file name; dem_file_path() and dem_base_name() recover
the real file path and a file-name-safe base name from it.
"""

import os
import re
import warnings

import rasterio

# Variable names tried, in order, when a multi-variable file is given without --variable.
DEFAULT_ELEVATION_VARIABLES = ("elev", "elevation", "z")

# Files whose variables resolve_dem_source() looks into. Anything else is passed through
# untouched, so it is only opened when it is validated.
NETCDF_EXTENSIONS = (".nc", ".nc4")

# 'DRIVER:"path":variable' or 'DRIVER:path:variable'. The driver name must be at least two
# characters so a Windows drive letter ('C:\...') isn't read as one.
_SUBDATASET_RE = re.compile(
    r'^(?P<driver>[A-Za-z0-9_]{2,}):(?:"(?P<qpath>[^"]+)"|(?P<path>[^:]+)):(?P<var>.+)$',
)


class DEMVariableError(ValueError):
    """The DEM variable asked for (or any default elevation variable) isn't in the file."""


def _parse_subdataset(dem_name):
    """Return (driver, file path, variable) for a subdataset string, or None for a plain path."""
    m = _SUBDATASET_RE.match(dem_name)
    if m is None:
        return None
    return m["driver"], m["qpath"] or m["path"], m["var"]


def _variable_name(var):
    """Return a subdataset's variable name without the leading '/' groups HDF5 puts on it."""
    return var.strip("/").rsplit("/", 1)[-1]


def dem_file_path(dem_name):
    """Return the file on disk that a DEM path or subdataset string refers to."""
    parsed = _parse_subdataset(dem_name)
    return dem_name if parsed is None else parsed[1]


def dem_base_name(dem_name):
    """Return the base name used for a DEM's output files.

    For a plain file this is the file name without its extension. For a subdataset the
    variable name is appended ('dem_elev'), so two variables of one file don't share outputs.
    """
    base = os.path.splitext(os.path.basename(dem_file_path(dem_name)))[0]
    parsed = _parse_subdataset(dem_name)
    if parsed is None:
        return base
    return base + "_" + parsed[2].strip("/").replace("/", "_")


def _is_netcdf_file(dem_name):
    """Return True for a plain NetCDF file path (not a subdataset string)."""
    return (
        _parse_subdataset(dem_name) is None
        and os.path.splitext(dem_name)[1].lower() in NETCDF_EXTENSIONS
    )


def possible_base_names(dem_name, variable=None):
    """Return every base name this DEM's outputs could have, without opening the file.

    A NetCDF file's outputs are named after the file alone when it has one variable, and
    with the chosen variable appended when it has several (see resolve_dem_source()).
    Which one applies isn't known until the file is opened, so for a NetCDF file this
    returns both: the variable given, or every DEFAULT_ELEVATION_VARIABLES name if None.
    """
    base = dem_base_name(dem_name)
    if not _is_netcdf_file(dem_name):
        return [base]
    names = [variable] if variable is not None else list(DEFAULT_ELEVATION_VARIABLES)
    return [base] + [f"{base}_{name}" for name in names]


def resolve_dem_source(dem_name, variable=None):
    """Return the path or subdataset string to open for this DEM.

    Args:
        dem_name: A raster file path, or an already-resolved subdataset string. Files
            without a NETCDF_EXTENSIONS extension are returned unchanged.
        variable: The variable to read from a NetCDF file. If None, a file with one
            variable is used as-is, and a file with several is searched for
            DEFAULT_ELEVATION_VARIABLES in order.

    Raises:
        FileNotFoundError: if the file doesn't exist.
        DEMVariableError: if the variable (or, with no variable given, every default name)
            isn't in the file.

    """
    if variable is None and _parse_subdataset(dem_name) is not None:
        return dem_name

    file_path = dem_file_path(dem_name)
    if not _is_netcdf_file(file_path):
        return dem_name
    if not os.path.exists(file_path):
        msg = f"Could not find DEM file {file_path}."
        raise FileNotFoundError(msg)

    # A multi-variable file has no georeferencing of its own, which rasterio warns about.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(file_path) as ds:
            count = ds.count
            subdatasets = list(ds.subdatasets)
            single_var = ds.tags(1).get("NETCDF_VARNAME") if count else None

    # {variable name: subdataset string to open}
    candidates = {}
    for sds in subdatasets:
        parsed = _parse_subdataset(sds)
        if parsed is not None:
            driver, _, var = parsed
            candidates[_variable_name(var)] = (
                f'{driver.upper()}:"{os.path.abspath(file_path)}":{var}'
            )

    if variable is not None:
        if variable in candidates:
            return candidates[variable]
        if not candidates and count and variable == single_var:
            return file_path
        available = list(candidates) or ([single_var] if single_var else [])
        msg = f"{file_path} has no variable '{variable}'. " + (
            f"Variables found: {', '.join(available)}."
            if available
            else "It has no named variables to choose from."
        )
        raise DEMVariableError(msg)

    if count or not candidates:
        return file_path

    for name in DEFAULT_ELEVATION_VARIABLES:
        if name in candidates:
            return candidates[name]

    msg = (
        f"{file_path} has several variables ({', '.join(candidates)}) and none of the "
        f"default elevation names ({', '.join(DEFAULT_ELEVATION_VARIABLES)}). "
        "Use --variable to name the one to validate."
    )
    raise DEMVariableError(msg)
