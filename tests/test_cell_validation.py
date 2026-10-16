"""Tests for the per-cell statistics a cell-validation worker computes for a chunk.

These run in-process on hand-built photons. Spawning the workers and handing them
chunks is covered by test_cell_workers.py.
"""

import numpy as np
import pytest

from ivert import validate_dem
from ivert.photon_classes import SEAFLOOR

EMPTY_VAL = -99999.0
GROUND = 1


def _validate(cells, *, min_photons=1, photon_limit=None, dem_elev=0.0):
    """Validate one chunk whose cells hold the given (heights, codes) photons."""
    heights = np.concatenate([np.asarray(h, dtype=np.float32) for h, _ in cells])
    codes = np.concatenate([np.asarray(c, dtype=np.int8) for _, c in cells])
    ends = np.cumsum([len(h) for h, _ in cells])
    n = len(cells)
    return validate_dem._validate_chunk(
        heights,
        codes,
        None,
        dem_i_list=np.arange(n),
        dem_j_list=np.zeros(n, dtype=int),
        dem_elev_list=np.full(n, dem_elev),
        cell_bounds=None,
        ph_ranges=(ends - [len(h) for h, _ in cells], ends),
        photon_limit=photon_limit,
        min_photons=min_photons,
        num_subdivisions=15,
        empty_val=EMPTY_VAL,
    )


def _ground(heights):
    return heights, [GROUND] * len(heights)


def test_cells_below_min_photons_are_left_out():
    """Too few photons means no result for the cell, not an empty or zero row."""
    results = _validate([_ground([1.0, 2.0]), _ground([1.0, 2.0, 3.0])], min_photons=3)

    assert list(results.index) == [(1, 0)]


def test_a_full_cell_ignores_photons_outside_its_interdecile_range():
    """One stray photon must not drag the mean of a well-sampled cell."""
    heights = [10.0] * 10 + [1000.0]
    results = _validate([_ground(heights)]).iloc[0]

    assert results["mean"] == pytest.approx(10.0)
    assert results["numphotons"] == len(heights)
    assert results["numphotons_intd"] == len(heights) - 1


def test_a_sparse_cell_uses_every_photon_and_has_no_interdecile_range():
    """Below INTERDECILE_MIN_PHOTONS, too few photons to call any one an outlier."""
    heights = [1.0, 2.0, 6.0]
    assert len(heights) < validate_dem.INTERDECILE_MIN_PHOTONS

    results = _validate([_ground(heights)]).iloc[0]

    assert results["mean"] == pytest.approx(3.0)
    assert results["numphotons_intd"] == len(heights)
    assert results["10p"] == results["90p"] == results["interdecile_range"] == EMPTY_VAL


def test_one_photon_gives_a_mean_but_no_spread():
    """A lone photon still validates its cell, with a NaN standard deviation."""
    results = _validate([_ground([4.0])]).iloc[0]

    assert results["mean"] == 4.0
    assert np.isnan(results["stddev"])


def test_the_error_is_dem_minus_icesat2():
    """A DEM above the photons gives a positive error."""
    results = _validate([_ground([1.0, 1.0, 1.0])], dem_elev=3.0).iloc[0]

    assert results["diff_mean"] == pytest.approx(2.0)


def test_bathymetry_photons_are_counted():
    """The bathy count decides whether a cell is filtered as land or bathymetry."""
    results = _validate([([1.0, 2.0, 3.0], [GROUND, SEAFLOOR, SEAFLOOR])]).iloc[0]

    assert results["numphotons_bathy"] == 2


def test_a_cell_over_the_photon_limit_is_sampled_down_to_it():
    """The limit caps the photons used per cell, whatever the cell holds."""
    limit = 4
    results = _validate([_ground(np.arange(50.0))], photon_limit=limit).iloc[0]

    assert results["numphotons"] == limit


def test_coverage_counts_the_sub_cells_holding_photons():
    """Coverage is the share of a cell's sub-grid that its photons fall in."""
    bounds = (0.0, 1.0, -1.0, 0.0)  # xmin, xmax, ymin, ymax of a north-up cell
    n = 4
    centers = (np.arange(n) + 0.5) / n
    xs, ys = np.meshgrid(centers, -centers)

    every_sub_cell = validate_dem._cell_coverage_fraction(
        xs.ravel(),
        ys.ravel(),
        bounds,
        n,
    )
    two_corners = validate_dem._cell_coverage_fraction(
        np.array([0.1, 0.9, 0.9]),
        np.array([-0.1, -0.9, -0.95]),
        bounds,
        n,
    )

    assert every_sub_cell == 1.0
    assert two_corners == 2 / n**2


def test_each_cell_range_holds_its_own_photons_in_their_order():
    """A worker reads a cell's photons as one slice of the sorted arrays."""
    rng = np.random.default_rng(0)
    ph_i = rng.integers(0, 3, 200)
    ph_j = rng.integers(0, 4, 200)
    cell_i, cell_j = np.divmod(np.arange(12), 4)
    # A cell with no photons at all.
    keep = ~((ph_i == 1) & (ph_j == 2))
    ph_i, ph_j = ph_i[keep], ph_j[keep]

    order, starts, ends = validate_dem._photon_cell_ranges(ph_i, ph_j, cell_i, cell_j)

    for k in range(len(cell_i)):
        expected = np.flatnonzero((ph_i == cell_i[k]) & (ph_j == cell_j[k]))
        np.testing.assert_array_equal(order[starts[k] : ends[k]], expected)
    assert starts[6] == ends[6]
