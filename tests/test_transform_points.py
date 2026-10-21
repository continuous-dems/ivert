"""Moving points between CRSs: horizontal reprojection and vertical datum shifts.

transformez builds vertical shift grids from downloaded datum models, so every test
here replaces transformez.generate_grid with a stand-in that writes a small, made-up
GeoTIFF the way transformez does: pixel-is-area, values at the pixel centres.
"""

import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import ivert.transform_points as tp

# lon -105 is the central meridian of UTM zone 13N, so it maps to easting 500 000.
LON, LAT = -105.0, 40.0
UTM_X, UTM_Y = 500_000.0, 4_427_757.2187
NODATA = -9999.0

ShiftFunc = Callable[[np.ndarray, np.ndarray], np.ndarray]


def _constant(value: float) -> ShiftFunc:
    return lambda lon, _lat: np.full_like(lon, value)


def _write_grid(path: str, region: list[float], shift: ShiftFunc) -> np.ndarray:
    """Write a 10 x 10 shift grid over region [w, e, s, n]; return its values."""
    size = 10
    w, e, s, n = region
    res_x, res_y = (e - w) / size, (n - s) / size
    centre_lons = w + (np.arange(size) + 0.5) * res_x
    centre_lats = n - (np.arange(size) + 0.5) * res_y  # top row first
    data = np.asarray(shift(*np.meshgrid(centre_lons, centre_lats)), dtype="float32")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(w, n, res_x, res_y),
        nodata=NODATA,
    ) as dst:
        dst.write(data, 1)
    return data


@dataclasses.dataclass
class FakeGrids:
    """What the stand-in generate_grid writes, and the calls it received."""

    cache: Path
    shift: ShiftFunc = dataclasses.field(default_factory=lambda: _constant(2.5))
    # Write the grid over this region instead of the one asked for.
    region_override: list[float] | None = None
    # Fail, as transformez does when it cannot build the grid.
    returns_none: bool = False
    calls: list[dict] = dataclasses.field(default_factory=list)


@pytest.fixture
def fake_grids(monkeypatch, tmp_path) -> FakeGrids:
    """Replace transformez.generate_grid with one that writes a made-up grid."""
    grids = FakeGrids(cache=tmp_path / "cache")

    def fake_generate_grid(
        *,
        region: list[float],
        out_fn: str,
        **kwargs: object,
    ) -> np.ndarray | None:
        grids.calls.append({"region": list(region), "out_fn": out_fn, **kwargs})
        if grids.returns_none:
            return None
        return _write_grid(out_fn, grids.region_override or list(region), grids.shift)

    monkeypatch.setattr(tp.transformez, "generate_grid", fake_generate_grid)
    return grids


def _shift(
    x: list[float],
    y: list[float],
    z: list[float],
    *,
    src: str = "EPSG:4326+4979",
    dst: str = "EPSG:4326+3855",
    cache: Path | None = None,
    src_region: list[float] | None = None,
) -> tuple:
    return tp.transform_points(
        np.asarray(x, dtype=float),
        np.asarray(y, dtype=float),
        np.asarray(z, dtype=float),
        src,
        dst,
        cache_dir=None if cache is None else str(cache),
        src_region=src_region,
    )


# --- No transform, horizontal only ------------------------------------------------


def test_the_same_crs_on_both_sides_returns_the_points_unchanged(fake_grids):
    """Nothing to do: the points come back as given, and no grid is built."""
    x, y, z = [LON], [LAT], [10.0]
    out = tp.transform_points(x, y, z, "EPSG:4326+3855", "EPSG:4326+3855")
    assert out == (x, y, z)
    assert fake_grids.calls == []


def test_a_horizontal_only_transform_reprojects_and_keeps_heights(fake_grids):
    """Lon/lat to UTM moves x and y, leaves z alone, and builds no grid."""
    x, y, z = _shift([LON], [LAT], [10.0], src="EPSG:4326", dst="EPSG:32613")
    np.testing.assert_allclose(x, [UTM_X], atol=1e-3)
    np.testing.assert_allclose(y, [UTM_Y], atol=1e-3)
    np.testing.assert_array_equal(z, [10.0])
    assert fake_grids.calls == []


def test_the_same_vertical_datum_needs_no_shift_grid(fake_grids):
    """A change of horizontal CRS alone leaves heights in the same datum."""
    _x, _y, z = _shift(
        [LON],
        [LAT],
        [10.0],
        src="EPSG:4326+3855",
        dst="EPSG:32613+3855",
    )
    np.testing.assert_array_equal(z, [10.0])
    assert fake_grids.calls == []


# --- Vertical shifts ----------------------------------------------------------------


def test_a_vertical_shift_adds_the_grid_value_to_each_height(fake_grids):
    """Each height gains the grid's shift; x and y are untouched."""
    x, y, z = _shift(
        [LON, LON + 0.03],
        [LAT, LAT + 0.04],
        [10.0, -5.0],
        cache=fake_grids.cache,
    )
    np.testing.assert_allclose(z, [12.5, -2.5])
    np.testing.assert_array_equal(x, [LON, LON + 0.03])
    np.testing.assert_array_equal(y, [LAT, LAT + 0.04])
    (call,) = fake_grids.calls
    assert (call["datum_in"], call["datum_out"]) == ("4979", "3855")


def test_horizontal_and_vertical_transforms_together(fake_grids):
    """The points are reprojected and shifted by the grid at their source lon/lat."""
    fake_grids.shift = _constant(3.0)
    x, y, z = _shift(
        [LON],
        [LAT],
        [10.0],
        dst="EPSG:32613+5703",
        cache=fake_grids.cache,
    )
    np.testing.assert_allclose(x, [UTM_X], atol=1e-3)
    np.testing.assert_allclose(y, [UTM_Y], atol=1e-3)
    np.testing.assert_allclose(z, [13.0])
    (call,) = fake_grids.calls
    assert (call["datum_in"], call["datum_out"]) == ("4979", "5703")


def test_a_transformez_reference_id_as_the_vertical_datum(fake_grids):
    """'vdatum:mllw' reaches transformez whole, and the ':' stays out of file names."""
    _x, _y, z = _shift(
        [LON],
        [LAT],
        [1.0],
        src="EPSG:4326+vdatum:mllw",
        cache=fake_grids.cache,
    )
    np.testing.assert_allclose(z, [3.5])
    (call,) = fake_grids.calls
    assert call["datum_in"] == "vdatum:mllw"
    grid_name = Path(call["out_fn"]).name
    assert ":" not in grid_name
    assert "vdatum_mllw" in grid_name


def test_the_shift_is_interpolated_between_grid_values(fake_grids):
    """A shift that varies linearly across the grid is recovered exactly anywhere.

    transformez samples each grid value at its pixel centre, so a grid read as if
    its values sat on its edges is stretched by half a pixel at each side.
    """
    fake_grids.shift = lambda lon, lat: 100.0 * (lon - LON) + 50.0 * (lat - LAT)
    lons = np.array([-104.987, -104.95, -104.913])
    lats = np.array([40.021, 40.05, 40.079])
    _x, _y, z = _shift(lons, lats, np.zeros(3), cache=fake_grids.cache)
    expected = 100.0 * (lons - LON) + 50.0 * (lats - LAT)
    np.testing.assert_allclose(z, expected, atol=1e-4)


def test_points_near_the_grid_edge_are_still_shifted(fake_grids):
    """Points between the grid's edge and its outer pixel centres get a shift."""
    # The grid spans lon -105.0 to -104.9 in 0.01-degree pixels; the first and last
    # pixel centres are at -104.995 and -104.905.
    lons = np.array([-104.9999, -104.998, -104.902, -104.9001])
    _x, _y, z = _shift(lons, np.full(4, 40.05), np.zeros(4), cache=fake_grids.cache)
    np.testing.assert_allclose(z, 2.5)


def test_shift_grids_are_cached_in_the_working_directory_by_default(
    fake_grids,
    tmp_path,
    monkeypatch,
):
    """With no cache_dir, grids go in ./transformez_cache."""
    monkeypatch.chdir(tmp_path)
    _shift([LON], [LAT], [0.0])
    (call,) = fake_grids.calls
    assert call["cache_dir"] == str(tmp_path / "transformez_cache")
    assert Path(call["out_fn"]).parent == tmp_path / "transformez_cache"


# --- The grid cache -----------------------------------------------------------------


def test_a_cached_grid_is_reused_for_points_inside_it(fake_grids):
    """Runs over slightly different extents inside one 0.1-degree box share a grid."""
    _shift([-104.98, -104.95], [40.02, 40.05], [0.0, 0.0], cache=fake_grids.cache)
    _x, _y, z = _shift([-104.97], [40.03], [1.0], cache=fake_grids.cache)
    np.testing.assert_allclose(z, [3.5])
    assert len(fake_grids.calls) == 1


def test_points_outside_the_cached_grid_get_a_new_one(fake_grids):
    """Points reaching past the cached grid's box are given a grid of their own."""
    _shift([-104.95], [40.05], [0.0], cache=fake_grids.cache)
    _shift([-104.95, -104.65], [40.05, 40.05], [0.0, 0.0], cache=fake_grids.cache)
    assert len(fake_grids.calls) == 2
    assert fake_grids.calls[0]["out_fn"] != fake_grids.calls[1]["out_fn"]


def test_a_cached_grid_that_falls_short_of_its_name_is_regenerated(fake_grids):
    """An older grid, named for the 0.1-degree box but covering less, is replaced."""
    fake_grids.region_override = [-104.98, -104.96, 40.02, 40.04]
    _shift([-104.97], [40.03], [0.0], cache=fake_grids.cache)

    fake_grids.region_override = None
    _x, _y, z = _shift([-104.95], [40.05], [1.0], cache=fake_grids.cache)
    np.testing.assert_allclose(z, [3.5])
    assert len(fake_grids.calls) == 2
    assert fake_grids.calls[0]["out_fn"] == fake_grids.calls[1]["out_fn"]


def test_an_unreadable_cached_grid_is_regenerated(fake_grids):
    """A truncated grid, as an interrupted run leaves, is built again."""
    _shift([-104.95], [40.05], [0.0], cache=fake_grids.cache)
    Path(fake_grids.calls[0]["out_fn"]).write_bytes(b"not a GeoTIFF")

    _x, _y, z = _shift([-104.95], [40.05], [1.0], cache=fake_grids.cache)
    np.testing.assert_allclose(z, [3.5])
    assert len(fake_grids.calls) == 2


@pytest.mark.parametrize(
    ("lons", "lats"),
    [
        ([-105.0], [40.0]),  # one point, both coordinates on 0.1-degree lines
        ([-105.0, -105.0], [40.0, 40.3]),  # a north-south line on a 0.1-degree line
    ],
)
def test_points_on_the_snapping_lines_still_get_a_grid_with_area(
    fake_grids,
    lons,
    lats,
):
    """Points with no extent in one direction still get a grid of nonzero size."""
    _x, _y, z = _shift(lons, lats, np.zeros(len(lons)), cache=fake_grids.cache)
    np.testing.assert_allclose(z, 2.5)
    w, e, s, n = fake_grids.calls[0]["region"]
    assert e > w
    assert n > s


def test_src_region_sets_the_grid_extent(fake_grids):
    """A given region, larger than the points, is what the grid is built to cover."""
    region = [-105.27, -104.83, 39.91, 40.36]
    _shift([LON], [LAT], [0.0], cache=fake_grids.cache, src_region=region)
    w, e, s, n = fake_grids.calls[0]["region"]
    assert w <= region[0]
    assert e >= region[1]
    assert s <= region[2]
    assert n >= region[3]


# --- Failures -----------------------------------------------------------------------


def test_a_grid_transformez_cannot_build_is_an_error(fake_grids):
    """Transformez returning no grid is a ValueError, not unshifted heights."""
    fake_grids.returns_none = True
    with pytest.raises(ValueError, match="Vertical transform failed"):
        _shift([LON], [LAT], [0.0], cache=fake_grids.cache)


def test_points_outside_the_grid_are_an_error_not_a_zero_shift(fake_grids):
    """A grid that comes back smaller than asked for must not leave points unshifted."""
    fake_grids.region_override = [-105.0, -104.95, 40.0, 40.05]
    with pytest.raises(ValueError, match="outside the shift grid"):
        _shift([-104.98, -104.91], [40.02, 40.02], [0.0, 0.0], cache=fake_grids.cache)


def test_heights_on_nodata_cells_come_back_nan(fake_grids, caplog):
    """A gap in the datum model leaves those heights NaN, with a warning."""
    fake_grids.shift = lambda _lon, lat: np.where(lat > 40.05, NODATA, 2.5)
    with caplog.at_level(logging.WARNING, logger=tp.__name__):
        _x, _y, z = _shift(
            [-104.95, -104.95],
            [40.09, 40.01],
            [1.0, 1.0],
            cache=fake_grids.cache,
        )
    assert np.isnan(z[0])
    np.testing.assert_allclose(z[1], 3.5)
    assert "nodata cells" in caplog.text
