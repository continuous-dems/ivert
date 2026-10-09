"""Tests for the photon class constants in ivert.photon_classes."""

from ivert import photon_classes


def test_named_constants_match_globato():
    """The constants are written out by hand, so a renumbering in globato would otherwise go unnoticed."""
    names = photon_classes.class_names()
    constants = {
        name: value
        for name, value in vars(photon_classes).items()
        if name.isupper() and isinstance(value, int)
    }

    assert constants
    for name, code in constants.items():
        assert names.get(code) == name.lower(), name
