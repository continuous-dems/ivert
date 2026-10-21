"""Tests for translating vertical datum names into references transformez can use."""

import pytest

from ivert import vdatum_lookup

# --- resolve_vdatum -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("navd88", "EPSG:5703"),
        ("NAVD88", "EPSG:5703"),
        ("  egm2008 ", "EPSG:3855"),
        ("EGM96", "EPSG:5773"),
        ("prvd02", "EPSG:6641"),
        ("cgvd2013", "EPSG:6647"),
        ("navd88 height (ft)", "EPSG:8228"),
    ],
)
def test_geoid_datum_names_resolve_to_epsg_codes(name, reference):
    """Names are case-insensitive and ignore surrounding spaces."""
    assert vdatum_lookup.resolve_vdatum(name) == reference


@pytest.mark.parametrize("name", ["ellipsoid", "wgs84", "WGS84"])
def test_ellipsoid_names_resolve_to_wgs84_3d(name):
    """Ellipsoidal heights mean WGS84, the photon database's frame (EPSG:4979)."""
    assert vdatum_lookup.resolve_vdatum(name) == "EPSG:4979"


@pytest.mark.parametrize(
    ("name", "reference"),
    [
        ("mllw", "vdatum:mllw"),
        ("MLW", "vdatum:mlw"),
        ("mtl", "vdatum:mtl"),
        ("dtl", "vdatum:dtl"),
        ("msl", "vdatum:msl"),
        ("mhw", "vdatum:mhw"),
        ("mhhw", "vdatum:mhhw"),
    ],
)
def test_tidal_datum_names_resolve_to_vdatum_references(name, reference):
    """Tidal datums go to transformez as its own VDatum references, not EPSG codes."""
    assert vdatum_lookup.resolve_vdatum(name) == reference


@pytest.mark.parametrize(
    ("code", "reference"),
    [
        (5703, "EPSG:5703"),
        ("5703", "EPSG:5703"),
        ("EPSG:5703", "EPSG:5703"),
        ("vdatum:mllw", "vdatum:mllw"),
        ("epsg:3855", "epsg:3855"),
    ],
)
def test_codes_and_qualified_references_pass_through(code, reference):
    """A bare code gains its 'EPSG:'; a qualified reference is left as given."""
    assert vdatum_lookup.resolve_vdatum(code) == reference


@pytest.mark.parametrize("name", [None, "", "not a datum", "navd89"])
def test_unknown_names_resolve_to_none(name):
    """A name the table doesn't know is None, not a guess."""
    assert vdatum_lookup.resolve_vdatum(name) is None


# --- check_vdatum -------------------------------------------------------------------


@pytest.mark.parametrize("reference", sorted(vdatum_lookup.list_vdatums()))
def test_every_listed_datum_is_one_transformez_can_use(reference):
    """Each reference a name resolves to passes the check, so -V accepts it."""
    vdatum_lookup.check_vdatum(reference)


@pytest.mark.parametrize(
    ("reference", "match"),
    [
        ("EPSG:4326", "no vertical component"),
        ("EPSG:5868", "no operation binding"),
        ("vdatum:xyz", "Unknown custom reference"),
        ("not a datum", "Unsupported coordinate reference"),
    ],
)
def test_references_transformez_cannot_use_are_a_value_error(reference, match):
    """Horizontal-only, untransformable and unknown references are all ValueErrors."""
    with pytest.raises(ValueError, match=match):
        vdatum_lookup.check_vdatum(reference)


# --- describe_vdatum and list_vdatums -----------------------------------------------


@pytest.mark.parametrize(
    ("reference", "description"),
    [
        ("EPSG:5703", "NAVD88 height"),
        ("vdatum:mtl", "NOAA VDatum Mean Tide Level"),
    ],
)
def test_references_are_described_by_transformez(reference, description):
    """The description is transformez's name for the vertical reference."""
    assert vdatum_lookup.describe_vdatum(reference) == description


@pytest.mark.parametrize("reference", ["EPSG:4326", "vdatum:xyz", "not a datum"])
def test_a_reference_with_no_vertical_name_is_described_as_empty(reference):
    """No vertical part, or one transformez doesn't know, gives an empty string."""
    assert vdatum_lookup.describe_vdatum(reference) == ""


def test_the_list_maps_each_reference_to_the_names_for_it():
    """Every listed name resolves to the reference it's listed under."""
    listed = vdatum_lookup.list_vdatums()
    assert "navd88" in listed["EPSG:5703"]
    assert listed["vdatum:mtl"] == ["mtl"]
    for reference, names in listed.items():
        assert names
        for name in names:
            assert vdatum_lookup.resolve_vdatum(name) == reference
