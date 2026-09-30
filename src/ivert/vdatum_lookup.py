"""Translate common vertical datum names into references transformez can use.

IVERT hands vertical datum shifts to transformez, which accepts EPSG codes and its
own reference IDs (such as ``vdatum:mllw``) but not the short names people type
(``navd88``, ``egm2008``). This module maps those names to a reference, and checks
that transformez can actually transform whatever reference it is given.
"""

# Geoid and geodetic datums: common name (lowercase) -> EPSG code.
_EPSG_NAMES: dict[str, int] = {
    # NAVD88
    "navd88": 5703,
    "navd88 height": 5703,
    "navd 88": 5703,
    "navd88 height (ft)": 8228,
    # Puerto Rico / Virgin Islands
    "prvd02": 6641,
    "prvd02 height": 6641,
    "vivd09": 6642,
    "vivd09 height": 6642,
    # Canada
    "cgvd2013": 6647,
    "cgvd2013(cgg2013)": 6647,
    "cgvd2013 height": 6647,
    # Global geoid models
    "egm2008": 3855,
    "egm2008 height": 3855,
    "egm 2008": 3855,
    "egm96": 5773,
    "egm96 height": 5773,
    "egm 96": 5773,
    # Ellipsoidal heights. These are 3D geographic CRSs, since EPSG has no
    # vertical-only CRS for ellipsoidal heights. "ellipsoid" means WGS84, to match
    # the photon databases (EPSG:4979).
    "ellipsoid": 4979,
    "wgs84": 4979,
    "itrf2014": 7912,
    # IGS14 is IGS's realization of ITRF2014 and agrees with it to within
    # millimetres. Its own code, EPSG:9018, has no transformez frame binding.
    "igs14": 7912,
}

# Tidal datums go to transformez as its own IDs rather than EPSG codes: it builds
# them from NOAA VDatum grids, and it is the authority on whether each surface is a
# height or a depth.
#
# transformez's VDatum support also lists the mean tide level (mtl) and diurnal
# tide level (dtl) surfaces, but it has no vdatum:mtl or vdatum:dtl reference to
# reach them through yet. Once it does, add "mtl" and "dtl" here.
_TIDAL_NAMES: dict[str, str] = {
    "mllw": "vdatum:mllw",
    "mlw": "vdatum:mlw",
    "msl": "vdatum:msl",
    "mhw": "vdatum:mhw",
    "mhhw": "vdatum:mhhw",
}


def resolve_vdatum(name: str | int | None) -> str | None:
    """Translate a vertical datum name or code into a reference transformez accepts.

    Args:
        name: A common name (``'navd88'``, ``'egm2008'``, ``'mllw'``), an EPSG code
            (``5703``, ``'5703'``, ``'EPSG:5703'``), or a transformez reference ID
            (``'vdatum:mllw'``). Names are case-insensitive.

    Returns:
        ``'EPSG:NNNN'`` for a geoid or geodetic datum, a transformez ID such as
        ``'vdatum:mllw'`` for a tidal datum, any other authority-qualified string
        unchanged, or None if the name is not recognised. The result is not checked
        against transformez; see check_vdatum().

    Examples::

        >>> resolve_vdatum("navd88")
        'EPSG:5703'
        >>> resolve_vdatum(5703)
        'EPSG:5703'
        >>> resolve_vdatum("MLLW")
        'vdatum:mllw'

    """
    if name is None:
        return None

    text = str(name).strip()
    if ":" in text:
        return text
    if text.isdecimal():
        return f"EPSG:{text}"

    key = text.lower()
    if key in _TIDAL_NAMES:
        return _TIDAL_NAMES[key]
    epsg = _EPSG_NAMES.get(key)
    return None if epsg is None else f"EPSG:{epsg}"


def _reference_errors() -> tuple[type[Exception], ...]:
    """Return the exceptions transformez raises for a reference it cannot use."""
    # transformez derives these from the built-in ReferenceError, not ValueError.
    from transformez.reference.parser import (
        InvalidReferenceError,
        ReferenceInputError,
        UnsupportedReferenceError,
    )

    return InvalidReferenceError, ReferenceInputError, UnsupportedReferenceError


def check_vdatum(reference: str) -> None:
    """Check that transformez can transform heights to or from a vertical reference.

    Args:
        reference: A reference as returned by resolve_vdatum().

    Raises:
        ValueError: If transformez cannot parse the reference, it has no vertical
            component, or transformez has no operation registered to transform it.
    """
    from transformez.reference.parser import parse_reference
    from transformez.reference.resolver import resolve_reference

    try:
        parsed = parse_reference(reference)
        if parsed.vertical is None:
            msg = f"{reference!r} has no vertical component."
            raise ValueError(msg)
        resolve_reference(parsed)
    except _reference_errors() as exc:
        raise ValueError(str(exc)) from exc


def describe_vdatum(reference: str) -> str:
    """Return transformez's name for a vertical reference, or an empty string."""
    from transformez.reference.parser import parse_reference

    try:
        vertical = parse_reference(reference).vertical
    except (*_reference_errors(), ValueError):
        return ""
    return vertical.name if vertical is not None else ""


def list_vdatums() -> dict[str, list[str]]:
    """Return each supported reference mapped to the common names that resolve to it."""
    by_reference: dict[str, list[str]] = {}
    for name in _EPSG_NAMES:
        by_reference.setdefault(resolve_vdatum(name), []).append(name)
    for name, reference in _TIDAL_NAMES.items():
        by_reference.setdefault(reference, []).append(name)
    return by_reference
