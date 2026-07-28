"""ivert.photon_classes
~~~~~~~~~~~~~~~~~~~~~~
Single source of truth for ICESat-2 photon classification codes.

The authoritative definitions live in globato's ``ATL03Reader.meta_desc``
docstring. Parsing them here keeps IVERT's CLI help, vector exports, and plot
legends in sync with the upstream classifier instead of each module carrying its
own (drift-prone) copy of the code list.
"""

import functools
import re


@functools.lru_cache(maxsize=1)
def photon_classes():
    """Return ``((code, description), ...)`` in the order globato lists them.

    Parsed from globato's ``ATL03Reader.meta_desc`` docstring. Raises
    ``ImportError`` if globato is not installed.
    """
    from globato.streams.readers.icesat2 import ATL03Reader

    classes = []
    for line in ATL03Reader.meta_desc.splitlines():
        match = re.match(r"^\s*(-?\d+)\s*:\s*(.+?)\s*$", line)
        if match:
            classes.append((int(match.group(1)), match.group(2)))
    return tuple(classes)


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
