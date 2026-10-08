"""Tests for validate_dem()'s recovery after the OS kills its sub-process (usually for memory).

The DEM is then split into 4 parts, each part is validated, and the parts' results are
merged. The sub-process is replaced by an in-process stand-in: the whole DEM's run is
"killed", and each part's run writes a small results file, so no photons are needed.
"""

import signal
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import rasterio
import rasterio.transform

from ivert import validate_dem
from ivert.utils.configfile import Config

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"),
    reason="The split only follows a SIGKILL, which this platform lacks.",
)

WIDTH = HEIGHT = 4


def _make_dem(path):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=WIDTH,
        height=HEIGHT,
        count=1,
        dtype="float32",
        crs="EPSG:4326+3855",
        transform=rasterio.transform.from_origin(-105.0, 40.0, 0.1, 0.1),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.ones((1, HEIGHT, WIDTH), dtype="float32"))
    return path


def _part_results(part_dem):
    """One results row per cell of the part, indexed by the part's own (i, j)."""
    with rasterio.open(part_dem) as ds:
        height, width = ds.height, ds.width
    i, j = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    n = i.size
    return pd.DataFrame(
        {
            "i": i.ravel(),
            "j": j.ravel(),
            "mean": np.ones(n),
            "diff_mean": np.linspace(-0.5, 0.5, n),
            "stddev": np.full(n, 0.1),
            "dem_elev": np.ones(n),
            "numphotons_intd": np.full(n, 10),
            "numphotons_bathy": np.zeros(n, dtype=int),
        },
    ).set_index(["i", "j"])


class _FakeProcess:
    """Stands in for mp.Process running validate_dem_parallel, without a sub-process."""

    whole_dem = None
    parts_have_results = True

    def __init__(self, target, args, kwargs) -> None:
        assert target is validate_dem.validate_dem_parallel
        self.dem_name = args[0]
        self.kwargs = kwargs
        self.exitcode = None

    def start(self):
        if self.dem_name == self.whole_dem:
            self.exitcode = -signal.SIGKILL
            return
        if self.parts_have_results:
            results_file = validate_dem._results_dataframe_filename(
                self.dem_name,
                self.kwargs["output_dir"],
            )
            _part_results(self.dem_name).to_hdf(results_file, key="icesat2")
            self.kwargs["shared_ret_values"]["results_dataframe_file"] = results_file
        self.exitcode = 0

    def join(self, timeout=None):
        pass

    def close(self):
        pass


@pytest.fixture
def dem(tmp_path, monkeypatch):
    """A 4x4 DEM whose validation is "killed" until it is split."""
    (tmp_path / "dems").mkdir()
    dem_path = _make_dem(tmp_path / "dems" / "dem.tif")
    _FakeProcess.whole_dem = str(dem_path)
    _FakeProcess.parts_have_results = True
    monkeypatch.setattr(validate_dem.mp, "Process", _FakeProcess)
    return dem_path


def _run(dem_path, output_dir):
    return validate_dem.validate_dem(
        str(dem_path),
        output_dir=output_dir,
        # The parts query no photons; passing a database skips opening the real one.
        icesat2_photon_database_obj=SimpleNamespace(config=Config()),
        export_error_formats=[],
    )


def test_the_parts_results_are_merged_into_the_whole_dems(dem, tmp_path):
    """The parts' results used to be dropped, and the DEM marked as having none."""
    out = tmp_path / "out"
    out.mkdir()

    _run(dem, out)

    merged = pd.read_hdf(out / "dem_results.h5")
    assert sorted(merged.index) == [(i, j) for i in range(HEIGHT) for j in range(WIDTH)]
    assert not (out / "dem_results_EMPTY.txt").exists()
    assert (out / "dem_summary_stats.txt").exists()
    assert (out / "dem_plot.png").exists()
    assert not validate_dem.dem_needs_validation(str(dem), out)


def test_a_dem_whose_parts_have_no_results_is_marked_for_reruns(dem, tmp_path):
    """The marker must have the name a rerun looks for, or the DEM is validated again."""
    _FakeProcess.parts_have_results = False
    out = tmp_path / "out"
    out.mkdir()

    _run(dem, out)

    assert not (out / "dem_results.h5").exists()
    assert (out / "dem_results_EMPTY.txt").read_text(encoding="utf-8") == (
        validate_dem._EMPTY_RESULTS_TEXT
    )
    assert not validate_dem.dem_needs_validation(str(dem), out)


def test_with_no_output_dir_the_results_go_beside_the_dem(dem):
    """No output folder means the DEM's own folder, as for a run that isn't split."""
    _run(dem, None)

    assert (dem.parent / "dem_results.h5").exists()
