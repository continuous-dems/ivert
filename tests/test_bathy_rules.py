"""Which bathymetry photons each filter rule flags, on a small made-up coast.

The reference bathymetry is a 10 x 10 grid of 0.01-degree cells (about 1.1 km)
over lon 0-0.1, lat 0-0.1: 10 m deep in its western half, 100 m deep in the east.
The landmask covers lon 0-0.08, with land in the strip west of lon 0.02, so the
coast runs north-south along lon 0.02. Every photon is at lat 0.05.
"""

import logging

import numpy as np
import pytest
import rasterio.transform
import shapely

from ivert import bathy_filters as bf

REFERENCE = np.where(np.arange(10) < 5, -10.0, -100.0)[np.newaxis, :].repeat(10, 0)
TRANSFORM = rasterio.transform.from_origin(0.0, 0.1, 0.01, 0.01)
LAND = shapely.box(0.0, 0.0, 0.02, 0.1)
COVERED = shapely.box(0.0, 0.0, 0.08, 0.1)
COAST = shapely.LineString([(0.02, 0.0), (0.02, 0.1)])


@pytest.fixture
def reads(monkeypatch):
    """Serve the made-up reference grid; record what was read."""
    read = []

    def load_reference(_raster, _bounds, _cache_dir):
        read.append("reference")
        return REFERENCE, TRANSFORM

    monkeypatch.setattr(bf, "_load_reference", load_reference)
    return read


def _evaluate(lons, heights, reads, **settings: object):
    """Flag photons at these longitudes and EGM2008 heights."""

    def get_landmask(_bounds):
        reads.append("landmask")
        return LAND, COVERED, COAST

    lon = np.asarray(lons, dtype=float)
    return bf._evaluate_rules(
        lon,
        np.full(len(lon), 0.05),
        np.asarray(heights, dtype=float),
        bf.BathyFilterSettings(**settings),
        cache_dir=None,
        get_landmask=get_landmask,
    )


def test_deep_flags_photons_where_the_reference_is_too_deep(reads):
    """Over the 100 m half, an ICESat-2 bathymetry photon is not believable."""
    flags = _evaluate([0.035, 0.055, 0.095], [-5, -5, -5], reads, rules=("deep",))

    assert flags["deep"].tolist() == [False, True, True]
    assert reads == ["reference"]


def test_deep_looks_for_the_shallowest_cell_in_its_window(reads):
    """A wider window reaches the shallow half, so the photon beside it is kept."""
    flags = _evaluate(
        [0.055, 0.095],
        [-5, -5],
        reads,
        rules=("deep",),
        ref_window_m=2000.0,
    )

    assert flags["deep"].tolist() == [False, True]


def test_reference_flags_photons_far_shallower_than_the_reference(reads):
    """5 m deep is fine over 10 m of water, not over 100 m."""
    flags = _evaluate([0.035, 0.095], [-5, -5], reads, rules=("reference",))

    assert flags["reference"].tolist() == [False, True]


def test_land_flags_deep_photons_on_land(reads):
    """On land, a photon far below sea level is noise; off it, the rule says nothing."""
    flags = _evaluate([0.01, 0.01, 0.05], [-20, -5, -20], reads, rules=("land",))

    assert flags["land"].tolist() == [True, False, False]
    assert reads == ["landmask"]


def test_offshore_flags_near_surface_photons_far_from_the_coast(reads):
    """Near the surface far out at sea is likely the surface, not the floor."""
    flags = _evaluate(
        # far out and shallow; far out and deep; near the coast; on land
        [0.07, 0.07, 0.022, 0.01],
        [-1, -20, -1, -1],
        reads,
        rules=("offshore",),
    )

    assert flags["offshore"].tolist() == [True, False, False, False]
    assert reads == ["landmask"]


def test_offshore_skips_photons_with_no_landmask_and_says_so(reads, caplog):
    """Where the landmask doesn't reach, the coast can't be judged."""
    with caplog.at_level(logging.WARNING):
        flags = _evaluate([0.09], [-1], reads, rules=("offshore",))

    assert flags["offshore"].tolist() == [False]
    assert "1 class-40 photons lie where no OSM landmask is available" in caplog.text


def test_offshore_can_spare_shallow_banks(reads):
    """With a minimum reference depth, a photon over the 10 m bank is kept."""
    lons, heights = [0.045, 0.07], [-1, -1]

    without = _evaluate(lons, heights, reads, rules=("offshore",))
    with_min = _evaluate(
        lons,
        heights,
        reads,
        rules=("offshore",),
        offshore_min_ref_depth_m=50.0,
    )

    assert without["offshore"].tolist() == [True, True]
    assert with_min["offshore"].tolist() == [False, True]


def test_only_the_enabled_rules_come_back(reads):
    """Each enabled rule has a flag array, in the settings' order; no others do."""
    flags = _evaluate(
        [0.01, 0.07],
        [-20, -1],
        reads,
        rules=("land", "deep", "offshore"),
    )

    assert list(flags) == ["deep", "offshore", "land"]
    assert all(mask.dtype == bool for mask in flags.values())
    assert sorted(reads) == ["landmask", "reference"]


def test_photons_off_the_reference_grid_are_not_flagged(reads):
    """With no reference value to compare against, NaN comparisons are False."""
    flags = _evaluate([0.5], [-5], reads, rules=("deep", "reference"))

    assert flags["deep"].tolist() == [False]
    assert flags["reference"].tolist() == [False]
