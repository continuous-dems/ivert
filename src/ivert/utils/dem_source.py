"""Work out which raster a DEM path refers to: a plain file, or one variable inside a NetCDF or HDF5 file.

A NetCDF or HDF5 file with a single data variable opens as an ordinary raster. One with
several variables opens with no bands of its own, so the elevation variable has to be
picked out as a GDAL subdataset string such as 'NETCDF:"/data/dem.nc":elev' or
'HDF5:"/data/dem.h5"://grid/elev'. The rest of IVERT passes that string around in place
of a file name; dem_file_path() and dem_base_name() recover the real file path and a
file-name-safe base name from it.

GDAL's HDF5 driver reads no coordinates, so an HDF5 file (often a NetCDF-4 file under
another name) is opened through the NetCDF driver when only that one georeferences it.
"""

import re
import warnings
from pathlib import Path

import rasterio

from ivert.utils.paths import absolute_path

# Variable names tried, in order, when a multi-variable file is given without --variable.
DEFAULT_ELEVATION_VARIABLES = ("elev", "elevation", "z")

# Files whose variables resolve_dem_source() looks into, and the GDAL driver prefix of
# their subdataset strings. Anything else is passed through untouched, so it is only
# opened when it is validated.
VARIABLE_FILE_DRIVERS = {
    ".nc": "NETCDF",
    ".nc4": "NETCDF",
    ".h5": "HDF5",
    ".hdf5": "HDF5",
}

# 'DRIVER:"path":variable' or 'DRIVER:path:variable'. The driver name must be at least two
# characters so a Windows drive letter ('C:\...') isn't read as one.
_SUBDATASET_RE = re.compile(
    r'^(?P<driver>[A-Za-z0-9_]{2,}):(?:"(?P<qpath>[^"]+)"|(?P<path>[^:]+)):(?P<var>.+)$',
)


class DEMSourceError(ValueError):
    """A DEM file can't be validated as given: see the subclasses."""


class DEMVariableError(DEMSourceError):
    """The DEM variable asked for (or any default elevation variable) isn't in the file."""


class DEMNotGeoreferencedError(DEMSourceError):
    """GDAL reads no geotransform for the DEM, so where it lies is unknown."""


def _parse_subdataset(dem_name):
    """Return (driver, file path, variable) for a subdataset string, or None for a plain path."""
    m = _SUBDATASET_RE.match(dem_name)
    if m is None:
        return None
    return m["driver"], m["qpath"] or m["path"], m["var"]


def _variable_path(var):
    """Return a subdataset's variable without the leading '/'s HDF5 puts on it ('grid/elev')."""
    return var.strip("/")


def dem_file_path(dem_name):
    """Return the file on disk that a DEM path or subdataset string refers to.

    A string, not a Path: it is the file part of the DEM name exactly as written, which
    callers find again inside the name (a Path would normalize './' and '//' away).
    """
    parsed = _parse_subdataset(dem_name)
    return dem_name if parsed is None else parsed[1]


def dem_base_name(dem_name):
    """Return the base name used for a DEM's output files.

    For a plain file this is the file name without its extension. For a subdataset the
    variable is appended ('dem_elev', or 'dem_grid_elev' for an HDF5 dataset in a group),
    so two variables of one file don't share outputs.
    """
    base = Path(dem_file_path(dem_name)).stem
    parsed = _parse_subdataset(dem_name)
    if parsed is None:
        return base
    return base + "_" + _variable_path(parsed[2]).replace("/", "_")


def dem_display_name(dem_name):
    """Return the name to show for a DEM in tables: its file name, plus any variable.

    A plain file gives its file name ('dem.tif'). A subdataset gives its file's name and
    the variable, without the GDAL driver prefix or quotes ('dem.nc:elev',
    'dem.h5:grid/elev'), so that two variables of one file stay apart.
    """
    file_name = Path(dem_file_path(dem_name)).name
    parsed = _parse_subdataset(dem_name)
    if parsed is None:
        return file_name
    return f"{file_name}:{_variable_path(parsed[2])}"


def _variable_file_driver(dem_name):
    """Return the subdataset driver prefix for a plain NetCDF or HDF5 path, else None."""
    if _parse_subdataset(dem_name) is not None:
        return None
    return VARIABLE_FILE_DRIVERS.get(Path(dem_name).suffix.lower())


def possible_base_names(dem_name, variable=None):
    """Return every base name this DEM's outputs could have.

    A NetCDF or HDF5 file's outputs are named after the file alone when it has one
    variable, and with the chosen variable appended when it has several (see
    resolve_dem_source()). This returns both: the variable given, or every
    DEFAULT_ELEVATION_VARIABLES name if None. With no variable given, an existing file
    is also opened to find the variable resolve_dem_source() would pick, since that may
    be none of the defaults' names (an HDF5 dataset in a group, 'grid/elev'); a file
    that can't be resolved adds nothing more.
    """
    base = dem_base_name(dem_name)
    if _variable_file_driver(dem_name) is None:
        return [base]
    names = [variable] if variable is not None else list(DEFAULT_ELEVATION_VARIABLES)
    bases = [base] + [
        f"{base}_{_variable_path(name).replace('/', '_')}" for name in names
    ]
    if variable is None and Path(dem_name).exists():
        try:
            resolved = dem_base_name(resolve_dem_source(dem_name))
        except (DEMSourceError, rasterio.errors.RasterioIOError):
            resolved = None
        if resolved is not None and resolved not in bases:
            bases.append(resolved)
    return bases


def _match_variable(name, candidates, file_path):
    """Return the subdataset string for variable 'name', or None if the file has none.

    'name' matches a variable's full path ('grid/elev'), or else the last part of it
    ('elev') if only one variable ends that way.

    Raises:
        DEMVariableError: if 'name' matches the last part of several variables' paths.
    """
    key = _variable_path(name)
    if key in candidates:
        return candidates[key]
    matches = [path for path in candidates if path.rsplit("/", 1)[-1] == key]
    if len(matches) > 1:
        msg = (
            f"{file_path} has several variables named '{key}' ({', '.join(matches)}). "
            "Use --variable with the full path of the one to validate."
        )
        raise DEMVariableError(msg)
    return candidates[matches[0]] if matches else None


def _opens(subdataset):
    """Return True if GDAL can open this subdataset string as a raster."""
    try:
        with rasterio.open(subdataset) as ds:
            return ds.count > 0
    except rasterio.errors.RasterioIOError:
        return False


def _georeferenced_source(subdataset):
    """Return the NetCDF form of an HDF5 subdataset string if only that one is georeferenced.

    GDAL's HDF5 driver never reads coordinate variables, so a NetCDF-4 file (which is an
    HDF5 file) with CF latitude/longitude or x/y coordinates opens through 'HDF5:' with
    no geotransform. Its 'NETCDF:' form reads the coordinates. Anything else, including
    an HDF5 file the NetCDF driver can't georeference either, is returned unchanged.
    """
    parsed = _parse_subdataset(subdataset)
    if parsed is None or parsed[0].upper() != "HDF5":
        return subdataset
    _, file_path, var = parsed
    netcdf = f'NETCDF:"{file_path}":{_variable_path(var)}'
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        try:
            with rasterio.open(netcdf) as ds:
                if ds.count > 0 and not ds.transform.is_identity:
                    return netcdf
        except rasterio.errors.RasterioIOError:
            pass
    return subdataset


def check_georeferenced(dem_name):
    """Raise DEMNotGeoreferencedError if GDAL reads no geotransform for this DEM.

    rasterio gives such a raster the identity transform, which would put its pixel
    indices in place of its coordinates: a DEM in degrees would seem to lie near
    0°N, 0°E and find no photons, with no sign of why.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(dem_name) as ds:
            ungeoreferenced = ds.transform.is_identity
    if ungeoreferenced:
        msg = (
            f"{dem_file_path(dem_name)} has no georeferencing (no geotransform), so "
            "where it lies is unknown. -p/--projection gives only its CRS, not its "
            "position; save the DEM in a format that records its grid coordinates "
            "(GeoTIFF, or NetCDF with coordinate variables)."
        )
        raise DEMNotGeoreferencedError(msg)


def _resolve_named_variable(
    variable,
    candidates,
    file_path,
    driver,
    single_var,
    single_variable_file,
):
    """Return what to open for a variable the user named, as resolve_dem_source() does.

    Args:
        variable: The variable the user named.
        candidates: {variable path: subdataset string} for a multi-variable file.
        file_path: The file's path.
        driver: Its subdataset driver prefix ('NETCDF' or 'HDF5').
        single_var: The variable name GDAL reports for a single-variable NetCDF file.
        single_variable_file: True if the file opened as a raster of its own.

    Raises:
        DEMVariableError: if the file has no such variable.

    """
    found = _match_variable(variable, candidates, file_path)
    if found is not None:
        return found
    if single_variable_file:
        # A single-variable file opens as a raster without listing its variable.
        if variable == single_var:
            return file_path
        prefix = "//" if driver == "HDF5" else ""
        subdataset = (
            f'{driver}:"{absolute_path(file_path)}":{prefix}{_variable_path(variable)}'
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
            if _opens(subdataset):
                return subdataset
    available = list(candidates) or ([single_var] if single_var else [])
    msg = f"{file_path} has no variable '{variable}'. " + (
        f"Variables found: {', '.join(available)}."
        if available
        else "It has no named variables to choose from."
    )
    raise DEMVariableError(msg)


def resolve_dem_source(dem_name, variable=None):
    """Return the path or subdataset string to open for this DEM.

    Args:
        dem_name: A raster file path, or an already-resolved subdataset string. Files
            without a VARIABLE_FILE_DRIVERS extension (NetCDF or HDF5) are returned
            unchanged.
        variable: The variable to read from a NetCDF or HDF5 file: a name ('elev') or,
            for HDF5, a path within the file ('grid/elev'). If None, a file with one
            variable is used as-is, and a file with several is searched for
            DEFAULT_ELEVATION_VARIABLES in order. A variable of an HDF5 file that
            GDAL can georeference only through its NetCDF driver (a NetCDF-4 file
            with coordinate variables) comes back as a 'NETCDF:' subdataset string.

    Raises:
        FileNotFoundError: if the file doesn't exist.
        DEMVariableError: if the variable (or, with no variable given, every default name)
            isn't in the file.

    """
    if variable is None and _parse_subdataset(dem_name) is not None:
        return dem_name

    file_path = dem_file_path(dem_name)
    driver = _variable_file_driver(file_path)
    if driver is None:
        return dem_name
    if not Path(file_path).exists():
        msg = f"Could not find DEM file {file_path}."
        raise FileNotFoundError(msg)
    abs_path = absolute_path(file_path)

    # A multi-variable file has no georeferencing of its own, which rasterio warns about.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", rasterio.errors.NotGeoreferencedWarning)
        with rasterio.open(file_path) as ds:
            count = ds.count
            subdatasets = list(ds.subdatasets)
            single_var = ds.tags(1).get("NETCDF_VARNAME") if count else None

    # {variable path: subdataset string to open}
    candidates = {}
    for sds in subdatasets:
        parsed = _parse_subdataset(sds)
        if parsed is not None:
            sds_driver, _, var = parsed
            candidates[_variable_path(var)] = f'{sds_driver.upper()}:"{abs_path}":{var}'

    if variable is not None:
        return _georeferenced_source(
            _resolve_named_variable(
                variable,
                candidates,
                file_path,
                driver,
                single_var if count else None,
                single_variable_file=not candidates and count > 0,
            ),
        )

    if count or not candidates:
        return file_path

    for name in DEFAULT_ELEVATION_VARIABLES:
        found = _match_variable(name, candidates, file_path)
        if found is not None:
            return _georeferenced_source(found)

    msg = (
        f"{file_path} has several variables ({', '.join(candidates)}) and none of the "
        f"default elevation names ({', '.join(DEFAULT_ELEVATION_VARIABLES)}). "
        "Use --variable to name the one to validate."
    )
    raise DEMVariableError(msg)
