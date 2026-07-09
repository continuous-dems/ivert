#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ivert.vdatum_lookup
~~~~~~~~~~~~~~~~~~~
Translate common vertical datum names to formal EPSG code strings.

Builds its table from ``transformez.definitions.Datums`` when that package is
importable, then merges in a curated fallback table of well-known aliases so
the most common names always resolve even without transformez installed.
"""

# ---------------------------------------------------------------------------
# Built-in fallback: common name (lowercase) → EPSG integer
# ---------------------------------------------------------------------------
_BUILTIN_TABLE = {
    # NAVD88
    "navd88": 5703,
    "navd88 height": 5703,
    "navd 88": 5703,
    "navd88 height (usft)": 6360,
    "navd88 height (ft)": 8228,
    # NGVD29
    "ngvd29": 5702,
    "ngvd 29": 5702,
    "ngvd29 height": 5702,
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
    # Tidal / surface datums
    "mllw": 5866,
    "mlw": 1091,
    "mhw": 5868,
    "mhhw": 5869,
    "msl": 5714,
    "mtl": 5713,
    # Great Lakes
    "igld85": 5609,
    "igld 85": 5609,
    "lwd_igld85": 9000,
    # Ellipsoidal reference (ITRF2014 / IGS14 / WGS84)
    "ellipsoid": 7912,
    "itrf2014": 7912,
    "igs14": 7912,
    "wgs84": 4979,
}

# Built-in fallback: EPSG integer → human-readable description
_BUILTIN_DESCRIPTIONS: dict[int, str] = {
    5703: "National Geodetic Vertical Datum 1988 (NAVD88)",
    6360: "NAVD88 height in US survey feet",
    8228: "NAVD88 height in international feet",
    5702: "National Geodetic Vertical Datum 1929 (NGVD29)",
    6641: "Puerto Rico Vertical Datum 2002 (PRVD02)",
    6642: "Virgin Islands Vertical Datum 2009 (VIVD09)",
    6647: "Canadian Geodetic Vertical Datum 2013 (CGVD2013, CGG2013 geoid)",
    3855: "Earth Gravitational Model 2008 (EGM2008)",
    5773: "Earth Gravitational Model 1996 (EGM96)",
    5866: "Mean Lower Low Water (MLLW) — USA tidal",
    1091: "Mean Low Water (MLW) — USA tidal",
    5868: "Mean High Water (MHW) — USA tidal",
    5869: "Mean Higher High Water (MHHW) — USA tidal",
    5714: "Mean Sea Level (MSL) — USA tidal",
    5713: "Mean Tide Level (MTL) — USA tidal",
    5609: "International Great Lakes Datum 1985 (IGLD85)",
    9000: "IGLD85 Low Water Datum (chart datum for Great Lakes)",
    7912: "Ellipsoidal height — ITRF2014 / IGS14 / WGS84",
    4979: "WGS84 ellipsoidal height",
}


# ---------------------------------------------------------------------------
# Optional: populate from transformez.definitions.Datums
# ---------------------------------------------------------------------------

def _build_tables_from_transformez():
    """Return (name_table, desc_table) built from transformez.Datums, or (None, None)."""
    try:
        from transformez.definitions import Datums  # type: ignore[import]
    except ImportError:
        return None, None

    name_table: dict[str, int] = {}
    desc_table: dict[int, str] = {}

    for epsg, info in {**Datums.CDN, **Datums.SURFACES}.items():
        if epsg == 0:
            continue

        raw_name = info.get("name", "")
        description = info.get("description", "")

        # Description: prefer the explicit "description" field; fall back to "name"
        desc_table[epsg] = description if description else raw_name

        if not raw_name:
            continue

        normalized = raw_name.lower().strip()
        name_table[normalized] = epsg

        # "navd88 height" → also map "navd88"
        without_height = normalized.removesuffix(" height").strip()
        if without_height and without_height != normalized:
            name_table.setdefault(without_height, epsg)

        # "cgvd2013(cgg2013)" → also map "cgvd2013"
        paren = normalized.find("(")
        if paren > 0:
            name_table.setdefault(normalized[:paren].strip(), epsg)

        # vdatum_id like "navd88:m:height" → map prefix "navd88"
        vdatum_id = info.get("vdatum_id", "")
        if vdatum_id:
            prefix = vdatum_id.split(":")[0].lower().strip()
            if prefix:
                name_table.setdefault(prefix, epsg)

    return name_table, desc_table


# ---------------------------------------------------------------------------
# Module-level cached tables (built once on first use)
# ---------------------------------------------------------------------------

_LOOKUP_TABLE: dict[str, int] | None = None
_DESC_TABLE: dict[int, str] | None = None


def _get_tables() -> tuple[dict[str, int], dict[int, str]]:
    global _LOOKUP_TABLE, _DESC_TABLE
    if _LOOKUP_TABLE is None:
        tz_names, tz_descs = _build_tables_from_transformez()

        name_table = tz_names or {}
        desc_table = tz_descs or {}

        # Merge builtin name table (setdefault keeps transformez values for names it provides)
        for key, epsg in _BUILTIN_TABLE.items():
            name_table.setdefault(key, epsg)

        # Merge builtin descriptions (builtin wins for EPSGs not described by transformez)
        for epsg, desc in _BUILTIN_DESCRIPTIONS.items():
            desc_table.setdefault(epsg, desc)

        _LOOKUP_TABLE = name_table
        _DESC_TABLE = desc_table

    return _LOOKUP_TABLE, _DESC_TABLE


def _get_lookup_table() -> dict[str, int]:
    return _get_tables()[0]


def _get_desc_table() -> dict[int, str]:
    return _get_tables()[1]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_vdatum(name: str | int | None) -> str | None:
    """Translate a common vertical datum name to a formal ``'EPSG:NNNN'`` string.

    Args:
        name: Common name (e.g. ``'navd88'``, ``'egm2008'``, ``'mllw'``),
              EPSG integer (e.g. ``5703``), bare integer string (``'5703'``),
              or authority-prefixed string (``'EPSG:5703'``).

    Returns:
        ``'EPSG:NNNN'`` on success, or ``None`` if the name is not recognised.
        Already-qualified authority strings (``'EPSG:4326+3855'``) are returned
        unchanged.

    Examples::

        >>> resolve_vdatum('navd88')
        'EPSG:5703'
        >>> resolve_vdatum('EGM2008 height')
        'EPSG:3855'
        >>> resolve_vdatum(5703)
        'EPSG:5703'
        >>> resolve_vdatum('EPSG:5703')
        'EPSG:5703'
    """
    if name is None:
        return None

    # Integer → EPSG:N
    if isinstance(name, int):
        return f"EPSG:{name}"

    name = str(name).strip()

    # Already authority-qualified (e.g. "EPSG:5703" or "EPSG:4326+3855")
    if ":" in name:
        return name

    # Bare integer string → EPSG:N
    try:
        return f"EPSG:{int(name)}"
    except ValueError:
        pass

    # Name lookup (case-insensitive)
    epsg = _get_lookup_table().get(name.lower())
    if epsg is not None:
        return f"EPSG:{epsg}"

    return None


def get_epsg_description(epsg: int) -> str:
    """Return a human-readable description for an EPSG code, or an empty string."""
    return _get_desc_table().get(epsg, "")


def list_vdatums() -> list[str]:
    """Return a sorted list of all recognised common datum names."""
    return sorted(_get_lookup_table().keys())
