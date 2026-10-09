"""Single source of truth for ICESat-2 photon classification codes.

The authoritative definitions are globato's ``PHOTON_CLASSES`` constant. Reading
them here keeps IVERT's CLI help, vector exports, and plot legends in sync with
the upstream classifier instead of each module carrying its own (drift-prone)
copy of the code list.

The named constants below are for code that tests one class by name. They are
written out here, not read from globato, because importing globato takes over a
second; ``tests/test_photon_classes.py`` checks that they match globato's codes.
"""

import re

UNCLASSIFIED = -1
NOISE = 0
GROUND = 1
CANOPY = 2
TOP_CANOPY = 3
LAND_ICE = 6
BUILDINGS = 7
SEAFLOOR = 40
NEARSHORE_WATER_SURFACE = 41
INLAND_WATER_SURFACE = 42


def photon_classes():
    """Return ``((code, description), ...)`` in the order globato lists them.

    Read from globato's ``PHOTON_CLASSES``. Raises ``ImportError`` if globato is
    not installed.
    """
    from globato.streams.readers.icesat2 import (  # noqa: PLC0415 - slow import
        PHOTON_CLASSES,
    )

    return tuple(PHOTON_CLASSES.items())


def class_descriptions():
    """Return ``{code: description}`` (the full upstream text) per class."""
    return dict(photon_classes())


def _short(description):
    """Strip parenthetical qualifiers and any '/'-separated alternates.

    e.g. ``"Seafloor (ATL24 / Dynamic Algo)"`` -> ``"Seafloor"``.
    """
    return re.sub(r"\(.*?\)", "", description).split("/")[0].strip()


def class_labels():
    """Return ``{code: short human label}``.

    For example, ``41 -> "Nearshore Water Surface"``.
    """
    return {code: _short(desc) for code, desc in photon_classes()}


def class_names():
    """Return ``{code: short snake_case name}``.

    For example, ``41 -> "nearshore_water_surface"``.
    """
    return {
        code: "_".join(_short(desc).split()).lower() for code, desc in photon_classes()
    }
