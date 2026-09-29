"""Single source of truth for ICESat-2 photon classification codes.

The authoritative definitions are globato's ``PHOTON_CLASSES`` constant. Reading
them here keeps IVERT's CLI help, vector exports, and plot legends in sync with
the upstream classifier instead of each module carrying its own (drift-prone)
copy of the code list.
"""

import re


def photon_classes():
    """Return ``((code, description), ...)`` in the order globato lists them.

    Read from globato's ``PHOTON_CLASSES``. Raises ``ImportError`` if globato is
    not installed.
    """
    from globato.streams.readers.icesat2 import PHOTON_CLASSES

    return tuple(PHOTON_CLASSES.items())


def class_descriptions():
    """Return ``{code: description}`` (the full upstream text) per class."""
    return dict(photon_classes())


def _short(description):
    """Strip parenthetical qualifiers and any '/'-separated alternates.

    e.g. ``"Coastline / Nearshore Water (ATL24 / Dynamic Algo)"`` -> ``"Coastline"``.
    """
    return re.sub(r"\(.*?\)", "", description).split("/")[0].strip()


def class_labels():
    """Return ``{code: short human label}``, e.g. ``41 -> "Coastline"``."""
    return {code: _short(desc) for code, desc in photon_classes()}


def class_names():
    """Return ``{code: short snake_case name}``, e.g. ``41 -> "coastline"``."""
    return {
        code: "_".join(_short(desc).split()).lower() for code, desc in photon_classes()
    }
