"""Tests for the photon class codes and names in ivert.photon_classes."""

import sys

import pytest

from ivert import photon_classes

GLOBATO_READER = "globato.streams.readers.icesat2"

# Some of globato's real descriptions, out of numeric order.
FAKE_CLASSES = {
    40: "Seafloor (ATL24 / Dynamic Algo)",
    1: "Ground (ATL08)",
    7: "Buildings (Dynamic Algo / Bing Mask)",
    41: "Nearshore Water Surface (ATL24 / Dynamic Algo)",
    -1: "Unclassified",
}


@pytest.fixture
def fake_classes(monkeypatch):
    """Give globato's PHOTON_CLASSES only the classes above, in that order."""
    module = pytest.importorskip(GLOBATO_READER)
    monkeypatch.setattr(module, "PHOTON_CLASSES", dict(FAKE_CLASSES))


def test_named_constants_match_globato():
    """The constants are hand-written, so a globato renumbering would go unnoticed."""
    names = photon_classes.class_names()
    constants = {
        name: value
        for name, value in vars(photon_classes).items()
        if name.isupper() and isinstance(value, int)
    }

    assert constants
    for name, code in constants.items():
        assert names.get(code) == name.lower(), name


def test_every_globato_class_has_a_label_and_a_distinct_name():
    """Each real class gets a non-empty label, and no two share a name."""
    labels = photon_classes.class_labels()
    names = photon_classes.class_names()
    assert labels.keys() == names.keys() == photon_classes.class_descriptions().keys()
    assert all(labels.values())
    assert len(set(names.values())) == len(names)


@pytest.mark.usefixtures("fake_classes")
def test_classes_come_in_globato_order():
    """photon_classes() keeps globato's order, not numeric order."""
    assert photon_classes.photon_classes() == tuple(FAKE_CLASSES.items())


@pytest.mark.usefixtures("fake_classes")
def test_descriptions_are_globato_text_unchanged():
    """class_descriptions() gives the full upstream text."""
    assert photon_classes.class_descriptions() == FAKE_CLASSES


@pytest.mark.usefixtures("fake_classes")
def test_labels_drop_qualifiers_and_alternates():
    """Labels lose their parenthetical qualifiers."""
    assert photon_classes.class_labels() == {
        40: "Seafloor",
        1: "Ground",
        7: "Buildings",
        41: "Nearshore Water Surface",
        -1: "Unclassified",
    }


@pytest.mark.usefixtures("fake_classes")
def test_names_are_snake_case_labels():
    """Names are the labels in lower case, with words joined by '_'."""
    assert photon_classes.class_names() == {
        40: "seafloor",
        1: "ground",
        7: "buildings",
        41: "nearshore_water_surface",
        -1: "unclassified",
    }


def test_without_globato_the_classes_raise_import_error(monkeypatch):
    """With globato missing, asking for the classes is an ImportError."""
    monkeypatch.setitem(sys.modules, GLOBATO_READER, None)
    with pytest.raises(ImportError):
        photon_classes.photon_classes()
